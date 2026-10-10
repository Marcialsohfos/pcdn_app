"""Lecture des .shp / .kml (et .zip contenant des .shp) -> GeoDataFrame WGS84, colonnes texte."""
from __future__ import annotations

import os
import re
import zipfile
from datetime import date, datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from lxml import etree
from shapely.geometry import LineString, Point, Polygon

from .util import norm_key


def list_sources(settings: dict, log) -> pd.DataFrame:
    """Décompresse les .zip non encore traités et renvoie les .shp/.kml à lire."""
    d_ftp, d_work = Path(settings["dir_ftp"]), Path(settings["dir_work"])
    for z in sorted(d_ftp.rglob("*.zip")):
        target = d_work / "unzipped" / f"{norm_key(z.name)}_{z.stat().st_size}"
        if not target.exists():
            target.mkdir(parents=True)
            try:
                with zipfile.ZipFile(z) as zf:
                    zf.extractall(target)
                    for info in zf.infolist():                       # restaure les dates d'origine (comme unzip en R)
                        f = target / info.filename
                        if f.exists() and not info.is_dir():
                            ts = datetime(*info.date_time).timestamp()
                            os.utime(f, (ts, ts))
            except Exception as e:
                log(f"ZIP illisible {z} : {e}", level="ERROR")
    files = []
    for root in (d_ftp, d_work / "unzipped"):
        if root.exists():
            files += [p for p in root.rglob("*") if p.suffix.lower() in (".shp", ".kml") and "__MACOSX" not in str(p)]
    return pd.DataFrame({"path": files, "ext": [p.suffix.lower().lstrip(".") for p in files],
                         "file_date": [pd.Timestamp(date.fromtimestamp(p.stat().st_mtime)) for p in files]})


# ---- KML : lecture XML directe (robuste aux ExtendedData de Mapit) ---------------------
def _coords(txt: str) -> list[tuple[float, float]]:
    out = []
    for t in re.sub(r"\s*,\s*", ",", txt.strip()).split():      # Mapit écrit parfois « lon, lat,alt » (espace après la virgule)
        p = t.split(",")
        out.append((float(p[0]), float(p[1])))
    return out


def _unquote(v):
    """Mapit exporte certaines valeurs entre guillemets ("Bitume") : on retire les guillemets enveloppants."""
    if isinstance(v, str):
        t = v.strip()
        if len(t) >= 2 and t[0] == t[-1] == '"':
            return t[1:-1]
    return v


def read_kml(path: Path) -> gpd.GeoDataFrame | None:
    root = etree.parse(str(path), etree.XMLParser(recover=True, huge_tree=True)).getroot()
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    pms = root.findall(".//Placemark")
    if not pms:
        return None
    rows, geoms = [], []
    for pm in pms:
        att = {"placemark_name": (pm.findtext("name") or None)}
        for d in pm.findall(".//ExtendedData//Data"):
            k = d.get("name")
            if k is not None:
                att[k] = d.findtext("value")
        for d in pm.findall(".//SimpleData"):
            if d.get("name"):
                att[d.get("name")] = d.text
        g = None
        try:
            pt, ln = pm.find(".//Point/coordinates"), pm.find(".//LineString/coordinates")
            pg = pm.find(".//Polygon/outerBoundaryIs//coordinates")
            if pt is not None:
                g = Point(_coords(pt.text)[0])
            elif ln is not None:
                g = LineString(_coords(ln.text))
            elif pg is not None:
                g = Polygon(_coords(pg.text))
        except Exception:
            g = None
        att["_multigeom"] = str(len(pm.xpath(".//Point|.//LineString|.//Polygon")) > 1)
        rows.append(att)
        geoms.append(g if g is not None else Point())          # géométrie vide si illisible
    keys = list(dict.fromkeys(k for r in rows for k in r))
    df = pd.DataFrame({k: pd.Series([r.get(k) for r in rows], dtype="object") for k in keys})
    return gpd.GeoDataFrame(df, geometry=geoms, crs=4326)


def _bad_utf8(df: pd.DataFrame) -> bool:
    return any("\ufffd" in str(v) for c in df.columns if c != "geometry" for v in df[c].tolist() if isinstance(v, str))


def read_shp(path: Path) -> tuple[gpd.GeoDataFrame, bool]:
    try:
        x = gpd.read_file(path, engine="pyogrio", encoding="UTF-8")
        bad = _bad_utf8(x)
    except Exception:
        x, bad = None, True
    if bad:
        x = gpd.read_file(path, engine="pyogrio", encoding="latin-1")
    assumed = x.crs is None
    x = x.set_crs(4326) if assumed else (x if x.crs.to_epsg() == 4326 else x.to_crs(4326))
    return x, assumed


def _make_unique(names: list[str]) -> list[str]:
    seen, out = {}, []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}.{seen[n]}")
        else:
            seen[n] = 0
            out.append(n)
    return out


def read_source(path: Path, file_date) -> tuple[gpd.GeoDataFrame, bool] | None:
    """-> (GeoDataFrame texte + _source + _file_date, crs_assumed) ou None."""
    try:
        if path.suffix.lower() == ".kml":
            x, assumed = read_kml(path), False
        else:
            x, assumed = read_shp(path)
    except Exception as e:
        raise RuntimeError(f"Lecture impossible {path.name} : {e}")
    if x is None or len(x) == 0:
        return None
    geom = x.geometry.name
    g = shapely.force_2d(np.array(x.geometry.values))
    d = x.drop(columns=[geom])
    d.columns = _make_unique(d.columns.tolist())               # shapefile : noms tronqués identiques -> nom, nom.1 (comme R)
    d = pd.DataFrame({c: pd.Series([None if (v is None or (isinstance(v, float) and np.isnan(v)) or v is pd.NA or v is pd.NaT)
                                    else str(v) for v in d[c].tolist()], dtype="object") for c in d.columns})
    d["_source"] = path.name
    d["_file_date"] = file_date
    d["_row_src"] = np.arange(1, len(d) + 1)                  # n° de ligne dans le fichier d'origine
    d["_fmt"] = path.suffix.lower().lstrip(".")
    return gpd.GeoDataFrame(d, geometry=g, crs=4326), assumed
