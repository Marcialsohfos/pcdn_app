"""Accès FTP/FTPS, découverte des fichiers, détection couche/zone, téléchargement."""
from __future__ import annotations

import ftplib
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import LAYERS
from .utils import norm

SHP_EXT = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".qpj", ".sbn", ".sbx"}
MAX_ZIP_BYTES = 2 * 1024**3


# ----------------------------------------------------------------- détection
def detect_layer(text: str) -> Optional[str]:
    t = re.sub(r"[^a-z0-9]+", "_", norm(text))
    best, best_len = None, 0
    for l in LAYERS.values():
        for a in (l.name.lower(),) + tuple(l.aliases):
            if a in t and len(a) > best_len:
                best, best_len = l.name, len(a)
    return best


def detect_zone(text: str) -> Optional[str]:
    m = re.search(r"zone[\s_\-]*0*(\d+)", text, re.I)
    return f"ZONE{int(m.group(1))}" if m else None


@dataclass
class SrcFile:
    path: str                       # chemin POSIX (distant, ou relatif à la racine locale)
    fmt: str                        # shp | kml | kmz | zip
    size: int
    layer: Optional[str]
    zone: Optional[str]
    sidecars: list = field(default_factory=list)
    include: bool = True


@dataclass
class Resolved:
    local: str
    fmt: str
    layer: Optional[str]
    zone: Optional[str]
    origin: str


def build_sources(entries: list[tuple[str, int]]) -> list[SrcFile]:
    shp: dict = {}
    out: list[SrcFile] = []
    for p, sz in entries:
        d, b = posixpath.split(p)
        stem, ext = posixpath.splitext(b)
        ext = ext.lower()
        if ext in SHP_EXT:
            shp.setdefault((d, stem.lower()), {})[ext] = (p, sz)
        elif ext in (".kml", ".kmz", ".zip"):
            out.append(SrcFile(p, ext[1:], sz, detect_layer(b) or detect_layer(p), detect_zone(p)))
    for (_d, _s), ex in shp.items():
        if ".shp" not in ex:
            continue
        main = ex[".shp"][0]
        out.append(SrcFile(main, "shp", sum(v[1] for v in ex.values()),
                           detect_layer(posixpath.basename(main)) or detect_layer(main), detect_zone(main),
                           [v[0] for k, v in ex.items() if k != ".shp"]))
    return sorted(out, key=lambda s: s.path)


# ----------------------------------------------------------------- FTP
class FtpClient:
    def __init__(self, host, user, pwd, port=21, tls=True, allow_plain=False, timeout=30):
        self.cfg = dict(host=host, user=user, pwd=pwd, port=int(port), tls=tls,
                        allow_plain=allow_plain, timeout=timeout)
        self.ftp: Optional[ftplib.FTP] = None
        self.mode = ""

    def connect(self):
        c = self.cfg
        last = None
        attempts = [True, False] if c["tls"] and c["allow_plain"] else [c["tls"]]
        for use_tls in attempts:
            try:
                ftp = ftplib.FTP_TLS(timeout=c["timeout"]) if use_tls else ftplib.FTP(timeout=c["timeout"])
                ftp.encoding = "utf-8"
                ftp.connect(c["host"], c["port"])
                ftp.login(c["user"], c["pwd"])
                if use_tls:
                    ftp.prot_p()
                ftp.set_pasv(True)
                self.ftp, self.mode = ftp, "FTPS" if use_tls else "FTP (non chiffré)"
                return self
            except ftplib.all_errors as e:  # noqa
                last = e
        raise ConnectionError(f"Connexion impossible à {c['host']}:{c['port']} — {last}")

    def close(self):
        try:
            if self.ftp:
                self.ftp.quit()
        except Exception:  # noqa
            pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, *a):
        self.close()

    def _ls(self, path):
        try:
            out = []
            for name, facts in self.ftp.mlsd(path, facts=["type", "size"]):
                t = facts.get("type", "")
                if name in (".", "..") or t in ("cdir", "pdir"):
                    continue
                out.append((name, t == "dir", int(facts.get("size", 0) or 0)))
            return out
        except (ftplib.error_perm, AttributeError):
            pass
        out = []
        for n in self.ftp.nlst(path):
            name = posixpath.basename(n.rstrip("/"))
            if name in (".", "..", ""):
                continue
            full = posixpath.join(path, name)
            cwd = self.ftp.pwd()
            try:
                self.ftp.cwd(full)
                is_dir = True
                self.ftp.cwd(cwd)
            except ftplib.error_perm:
                is_dir = False
            try:
                size = self.ftp.size(full) or 0 if not is_dir else 0
            except ftplib.all_errors:
                size = 0
            out.append((name, is_dir, size))
        return out

    def walk(self, root="/", max_depth=5, progress=None) -> list[tuple[str, int]]:
        res: list[tuple[str, int]] = []
        stack = [(root, 0)]
        while stack:
            path, depth = stack.pop()
            if progress:
                progress(f"Exploration {path}")
            try:
                items = self._ls(path)
            except ftplib.all_errors:
                continue
            for name, is_dir, size in items:
                full = posixpath.join(path, name)
                if is_dir:
                    if depth < max_depth:
                        stack.append((full, depth + 1))
                else:
                    res.append((full, size))
        return res

    def download(self, remote: str, local: Path, retries=3):
        last = None
        for i in range(retries):
            try:
                with open(local, "wb") as fh:
                    self.ftp.retrbinary(f"RETR {remote}", fh.write)
                return
            except ftplib.all_errors as e:  # noqa
                last = e
                time.sleep(1.5 * (i + 1))
                try:
                    self.connect()
                except ConnectionError:
                    pass
        raise IOError(f"Échec du téléchargement de {remote}: {last}")


# ----------------------------------------------------------------- matérialisation
def _safe_extract(zpath: Path, outdir: Path, prefix: str) -> list[tuple[str, int]]:
    entries = []
    with zipfile.ZipFile(zpath) as z:
        if sum(i.file_size for i in z.infolist()) > MAX_ZIP_BYTES:
            raise IOError(f"Archive trop volumineuse: {zpath.name}")
        for info in z.infolist():
            if info.is_dir():
                continue
            flat = re.sub(r"[^\w.\-]+", "_", info.filename.replace("/", "__"))
            dest = outdir / f"{prefix}{flat}"          # aplati => pas de zip-slip
            dest.write_bytes(z.read(info))
            entries.append((dest.name, info.file_size))
    return entries


def fetch(src: SrcFile, workdir: Path, ftp: Optional[FtpClient] = None, root: Optional[Path] = None) -> list[Resolved]:
    """Rend local les fichiers d'une source (FTP si `ftp`, sinon fichiers déjà présents sous `root`)."""
    workdir.mkdir(parents=True, exist_ok=True)
    tag = re.sub(r"\W+", "_", src.path)[-60:]
    sub = workdir / tag
    sub.mkdir(parents=True, exist_ok=True)

    def get(p: str) -> Path:
        if ftp is None:
            return Path(root) / p
        dst = sub / posixpath.basename(p)
        ftp.download(p, dst)
        return dst

    main = get(src.path)
    for sc in src.sidecars:
        try:
            get(sc)
        except Exception:  # noqa  (sidecars optionnels sauf .shx/.dbf, contrôlés à la lecture)
            pass
    if src.fmt != "zip":
        return [Resolved(str(main), src.fmt, src.layer, src.zone, src.path)]
    inner = build_sources(_safe_extract(main, sub, "z_"))
    res = []
    for s in inner:
        res.append(Resolved(str(sub / s.path), s.fmt, s.layer or src.layer, s.zone or src.zone,
                            f"{src.path}!{s.path}"))
    return res
