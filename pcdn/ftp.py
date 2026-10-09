"""Synchronisation FTP/FTPS : ne télécharge que les fichiers nouveaux ou modifiés (manifeste local)."""
from __future__ import annotations

import ftplib
import posixpath
import re
from datetime import datetime

import numpy as np
import pandas as pd

from .settings import env

_UNIX = re.compile(r"^([d-])[rwxsStT-]{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w{3}\s+\d+\s+[\d:]+)\s+(.+)$")


def ftp_cfg() -> dict:
    cfg = {"host": env("PCDN_FTP_HOST"), "user": env("PCDN_FTP_USER"), "pwd": env("PCDN_FTP_PWD"),
           "proto": env("PCDN_FTP_PROTO", "ftp").lower(), "port": env("PCDN_FTP_PORT")}
    if not (cfg["host"] and cfg["user"] and cfg["pwd"]):
        raise RuntimeError("Accès FTP incomplets : renseignez PCDN_FTP_HOST, PCDN_FTP_USER et PCDN_FTP_PWD "
                           "(variables d'environnement ou .streamlit/secrets.toml)")
    return cfg


def connect(cfg: dict) -> ftplib.FTP:
    host, port = cfg["host"], cfg["port"]
    if ":" in host and not port:
        host, port = host.rsplit(":", 1)
    tls = cfg["proto"] in ("ftps", "ftpes")
    ftp = ftplib.FTP_TLS(timeout=30) if tls else ftplib.FTP(timeout=30)
    ftp.connect(host, int(port) if port else 21)
    ftp.login(cfg["user"], cfg["pwd"])
    if tls:
        ftp.prot_p()
    ftp.set_pasv(True)
    return ftp


def list_dir(ftp: ftplib.FTP, path: str) -> list[dict]:
    ftp.cwd(path)
    try:
        out = []
        for name, f in ftp.mlsd(facts=["type", "size", "modify"]):
            if f.get("type") in ("cdir", "pdir") or name in (".", ".."):
                continue
            out.append({"name": name, "is_dir": f.get("type") == "dir",
                        "size": float(f["size"]) if "size" in f else np.nan, "mtime": f.get("modify")})
        return out
    except ftplib.Error:
        pass
    lines: list[str] = []
    ftp.retrlines("LIST", lines.append)
    lines = [l for l in lines if l.strip()]
    ms = [_UNIX.match(l) for l in lines]
    if lines and all(ms):
        return [{"name": m.group(4).strip(), "is_dir": m.group(1) == "d", "size": float(m.group(2)), "mtime": m.group(3)}
                for m in ms if m.group(4).strip() not in (".", "..")]
    names = [n for n in ftp.nlst() if n not in (".", "..")]            # repli : dossier = pas d'extension
    return [{"name": posixpath.basename(n), "is_dir": not re.search(r"\.[A-Za-z0-9]{2,4}$", n),
             "size": np.nan, "mtime": None} for n in names]


def walk(ftp, path: str, depth: int, max_depth: int, recursive: bool, log) -> list[dict]:
    items = list_dir(ftp, path)
    files = [{**i, "remote_dir": path} for i in items if not i["is_dir"]]
    if recursive and depth < max_depth:
        for d in [i["name"] for i in items if i["is_dir"]]:
            try:
                files += walk(ftp, posixpath.join(path, d), depth + 1, max_depth, recursive, log)
            except Exception as e:
                log(f"Dossier illisible {posixpath.join(path, d)} : {e}", level="WARN")
    return files


def sync(settings: dict, log, progress=None) -> int:
    cfg = ftp_cfg()
    d_ftp = settings["dir_ftp"]
    d_ftp.mkdir(parents=True, exist_ok=True)
    mpath = d_ftp / "_manifest.csv"
    cols = ["remote", "size", "mtime", "local", "downloaded_at"]
    manifest = pd.read_csv(mpath, dtype=str) if mpath.exists() else pd.DataFrame(columns=cols)
    ftp = connect(cfg)
    try:
        root = "/" + settings["ftp_path"].strip("/")
        remote = [f for f in walk(ftp, root, 0, settings["ftp_max_depth"], settings["ftp_recursive"], log)
                  if f["name"].rsplit(".", 1)[-1].lower() in settings["extensions"]]
        log(f"FTP : {len(remote)} fichier(s) pertinent(s) sur le serveur")
        n_new = 0
        for k, r in enumerate(remote):
            rpath = posixpath.join(r["remote_dir"], r["name"])
            known = manifest[manifest["remote"] == rpath]
            if len(known) == 1:
                kn = known.iloc[0]
                same_size = pd.isna(r["size"]) or str(kn["size"]) in (str(r["size"]), str(int(r["size"])))
                same_time = r["mtime"] is None or str(kn["mtime"]) == str(r["mtime"])
                if same_size and same_time and pd.notna(kn["local"]) and __import__("os").path.exists(kn["local"]):
                    continue
            sub = re.sub(r"[^A-Za-z0-9_-]+", "_", r["remote_dir"].strip("/"))
            ldir = d_ftp / sub if sub else d_ftp
            ldir.mkdir(parents=True, exist_ok=True)
            local = ldir / r["name"]
            try:
                ftp.cwd(r["remote_dir"])
                with open(local, "wb") as fh:
                    ftp.retrbinary("RETR " + r["name"], fh.write)
            except Exception as e:
                log(f"Échec téléchargement {rpath} : {e}", level="ERROR")
                continue
            n_new += 1
            manifest = manifest[manifest["remote"] != rpath]
            manifest.loc[len(manifest)] = [rpath, r["size"], r["mtime"], str(local), datetime.now().isoformat(timespec="seconds")]
            log(f"Téléchargé : {rpath}")
            if progress:
                progress((k + 1) / max(1, len(remote)))
    finally:
        try:
            ftp.quit()
        except Exception:
            pass
    manifest.to_csv(mpath, index=False)
    log(f"FTP : {n_new} fichier(s) nouveau(x) ou modifié(s) téléchargé(s)")
    return n_new
