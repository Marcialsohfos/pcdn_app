"""Lecture SHP / KML / KMZ, alignement sur le schéma, fusion."""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, MultiLineString, MultiPoint, Point

from .config import CRS_OUT, LAYERS, META_COLS
from .sources import Resolved
from .utils import is_null

os.environ.setdefault("SHAPE_RESTORE_SHX", "YES")


# ------------------------------------------------------------------ KML
def _ln(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _coords(text: str):
    pts = []
    for tok in (text or "").split():
        p = tok.split(",")
        if len(p) >= 2:
            try:
                pts.append((float(p[0]), float(p[1])))
            except ValueError:
                pass
    return pts


def _pm_geom(pm):
    geoms = []
    for el in pm.iter():
        n = _ln(el.tag)
        if n in ("Point", "LineString"):
            ce = next((c for c in el.iter() if _ln(c.tag) == "coordinates"), None)
            pts = _coords(ce.text) if ce is not None else []
            if n == "Point" and pts:
                geoms.append(Point(pts[0]))
            elif n == "LineString" and len(pts) >= 2:
                geoms.append(LineString(pts))
    if not geoms:
        return None
    if len(geoms) == 1:
        return geoms[0]
    if all(isinstance(g, Point) for g in geoms):
        return MultiPoint(geoms)
    return MultiLineString([g for g in geoms if isinstance(g, LineString)])


def read_kml(path: str) -> gpd.GeoDataFrame:
    p = Path(path)
    if p.suffix.lower() == ".kmz":
        with zipfile.ZipFile(p) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".kml")), None)
            if not name:
                raise ValueError("KMZ sans fichier .kml")
            data = z.read(name)
    else:
        data = p.read_bytes()
    root = ET.fromstring(data)
    rows, geoms = [], []
    for pm in (e for e in root.iter() if _ln(e.tag) == "Placemark"):
        props = {}
        for c in pm:
            if _ln(c.tag) == "name":
                props["name"] = (c.text or "").strip()
        for el in pm.iter():
            n = _ln(el.tag)
            if n == "Data" and el.get("name"):
                v = next((x for x in el if _ln(x.tag) == "value"), None)
                props[el.get("name")] = v.text if v is not None else None
            elif n == "SimpleData" and el.get("name"):
                props[el.get("name")] = el.text
        if len(props) <= 1:  # repli : tableau HTML dans <description>
            d = next((c.text for c in pm if _ln(c.tag) == "description" and c.text), "")
            for k, v in re.findall(r"<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", d, re.S | re.I):
                props[re.sub(r"<[^>]+>", "", k).strip()] = re.sub(r"<[^>]+>", "", v).strip()
        rows.append(props)
        geoms.append(_pm_geom(pm))
    return gpd.GeoDataFrame(pd.DataFrame(rows), geometry=geoms, crs="EPSG:4326")


# ------------------------------------------------------------------ SHP
def read_shp(path: str) -> gpd.GeoDataFrame:
    gdf = gpd.read_file(path, engine="pyogrio")
    txt = gdf.select_dtypes(include="object")
    if len(txt.columns) and txt.apply(lambda c: c.astype(str).str.contains("Ã|\ufffd", regex=True).any()).any():
        try:
            gdf = gpd.read_file(path, engine="pyogrio", encoding="cp1252")
        except Exception:  # noqa
            pass
    return gdf


def read_any(r: Resolved) -> gpd.GeoDataFrame:
    gdf = read_shp(r.local) if r.fmt == "shp" else read_kml(r.local)
    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_OUT)
    elif str(gdf.crs).upper() != CRS_OUT:
        gdf = gdf.to_crs(CRS_OUT)
    return gdf


# ------------------------------------------------------------------ alignement
def align_columns(gdf: gpd.GeoDataFrame, layer: str, fmt: str = "shp"):
    """Renomme (casse, troncature shapefile à 10 car.), ajoute les manquantes, écarte les inconnues."""
    spec = LAYERS[layer]
    exp = [f.name for f in spec.fields]
    cols = [c for c in gdf.columns if c != "geometry"]
    lower = {c.lower(): c for c in cols}
    rename, issues = {}, []

    pending = []
    for e in exp:
        if e in cols:
            continue
        if e.lower() in lower:
            rename[lower[e.lower()]] = e
        else:
            pending.append(e)
    used = set(rename) | set(exp)
    cand: dict[str, list[str]] = {}
    for e in pending:   # colonnes tronquées (≥ 8 car.) par le format shapefile
        for c in cols:
            if c not in used and len(c) >= 8 and e.lower().startswith(c.lower()):
                cand.setdefault(c, []).append(e)
    for c, es in cand.items():
        if len(es) == 1:
            rename[c] = es[0]
        else:
            issues.append(("STRUCT_AMBIGUOUS", "WARNING", c,
                           f"Colonne tronquée '{c}' ambiguë entre {es} — non mappée (exporter en KML ou renommer)."))
    for old, new in rename.items():
        if old.lower() != new.lower() or old != new:
            issues.append(("STRUCT_RENAMED", "INFO", new, f"Colonne '{old}' reconnue comme '{new}'."))
    gdf = gdf.rename(columns=rename)

    idf = spec.id_field
    if (idf not in gdf.columns or gdf[idf].map(is_null).all()) and "name" in gdf.columns:
        gdf[idf] = gdf["name"]
        issues.append(("STRUCT_ID_FROM_NAME", "INFO", idf, "Identifiant repris du <name> KML."))
    for e in exp:
        if e not in gdf.columns:
            gdf[e] = None
            # un KML n'exporte pas les champs vides : colonne absente = simple information
            issues.append(("STRUCT_MISSING_COL", "INFO" if fmt in ("kml", "kmz") else "ERROR", e,
                           f"Colonne attendue absente: '{e}'."))
    extra = [c for c in gdf.columns if c not in exp and c != "geometry" and c not in META_COLS]
    if extra:
        issues.append(("STRUCT_EXTRA_COL", "INFO", ", ".join(extra[:8]),
                       f"{len(extra)} colonne(s) hors schéma ignorée(s): {', '.join(extra[:8])}"))
    return gdf[exp + ["geometry"]], issues


def ingest(resolved: list[Resolved], log) -> tuple[dict, pd.DataFrame]:
    """-> ({couche: GeoDataFrame fusionné}, issues structurelles)."""
    groups: dict = {}
    for r in resolved:
        if not r.layer:
            log(f"⚠ Ignoré (couche non identifiée): {r.origin}")
            continue
        groups.setdefault((r.layer, r.zone), []).append(r)

    frames: dict[str, list] = {}
    struct = []
    for (layer, zone), rs in sorted(groups.items(), key=lambda x: (x[0][0], str(x[0][1]))):
        rs = sorted(rs, key=lambda r: r.fmt != "shp")  # SHP prioritaire, KML en secours
        gdf, used = None, None
        for r in rs:
            try:
                gdf = read_any(r)
                used = r
                break
            except Exception as e:  # noqa
                log(f"⚠ Lecture impossible {r.origin} ({r.fmt}): {e}")
        if gdf is None:
            struct.append((layer, zone, "STRUCT_UNREADABLE", "ERROR", "", f"Aucune source lisible pour {layer}/{zone}"))
            continue
        if len(rs) > 1:
            log(f"ℹ {layer}/{zone}: {len(rs)} formats trouvés, {used.fmt.upper()} retenu.")
        gdf, iss = align_columns(gdf, layer, used.fmt)
        struct += [(layer, zone, c, s, f, m) for c, s, f, m in iss]
        gdf["zone"] = zone
        gdf["source_file"] = Path(used.origin.split("!")[-1]).name
        gdf["source_fmt"] = used.fmt
        frames.setdefault(layer, []).append(gdf)
        log(f"✔ {layer} / {zone or 'zone ?'} : {len(gdf)} entités ({used.fmt.upper()})")

    merged = {}
    for layer, fl in frames.items():
        m = gpd.GeoDataFrame(pd.concat(fl, ignore_index=True), geometry="geometry", crs=CRS_OUT)
        m["_rid"] = range(len(m))
        merged[layer] = m
    sdf = pd.DataFrame(struct, columns=["layer", "zone", "rule", "severity", "field", "message"])
    return merged, sdf
