"""Pré-traitement : nettoyage, géométries, types, domaines, doublons, harmonisation des ID."""
from __future__ import annotations

import re
from dataclasses import dataclass

import geopandas as gpd
import pandas as pd
import shapely
from shapely.ops import linemerge

from .config import BBOX, CRS_OUT, ID_RE, LAYERS
from .qa import domain_lookup, match_domain
from .utils import clean_text, is_null, parse_bool, parse_num, split_multi


@dataclass
class PreOpts:
    clean_text: bool = True
    fix_geometry: bool = True       # 2D, make_valid, multi -> simple
    fix_swapped_xy: bool = True     # inverse lon/lat des points hors Cameroun si l'inversion tombe dedans
    drop_empty_geom: bool = True    # écartées dans « rejets »
    coerce_types: bool = True
    fix_domains: bool = True        # casse / accents / tirets
    fuzzy_cutoff: float = 0.88      # 0 = désactivé ; corrige les fautes de frappe proches
    drop_exact_dupes: bool = True
    harmonize_ids: bool = True
    add_coords: bool = True
    quarantine_errors: bool = False  # exclure les entités encore en ERREUR de l'export principal


def _canon_id(v, prefix):
    if is_null(v):
        return None
    s = re.sub(r"[\s_\-./]", "", str(v).upper())
    m = ID_RE.match(re.sub(r"(?<=\D)0+(?=\d)", "", s)) or ID_RE.match(s)
    if m and m.group(2) == prefix:
        return f"ZONE{int(m.group(1))}{prefix}{int(m.group(3))}"
    return None


def harmonize_ids(g: gpd.GeoDataFrame, layer: str, log) -> gpd.GeoDataFrame:
    spec = LAYERS[layer]
    idf, pfx = spec.id_field, spec.prefix
    g["id_orig"] = g[idf]
    canon = g[idf].map(lambda v: _canon_id(v, pfx))
    zid = canon.map(lambda c: f"ZONE{ID_RE.match(c).group(1)}" if isinstance(c, str) else None)
    zone = g["zone"].where(g["zone"].notna(), zid)
    bad = canon.isna() | canon.duplicated(keep="first") | (zid.notna() & zone.notna() & (zid != zone))
    g["zone"] = zone
    canon = [None if b else c for c, b in zip(canon, bad)]
    zone = [z if isinstance(z, str) else None for z in zone]
    nxt = {}
    for z in set(x for x in zone if x):
        nums = [int(ID_RE.match(c).group(3)) for c, zz in zip(canon, zone) if c and zz == z]
        nxt[z] = max(nums, default=0) + 1
    new_ids, renum = [], 0
    for c, z in zip(canon, zone):
        if c:
            new_ids.append(c)
        elif z:
            new_ids.append(f"{z}{pfx}{nxt[z]}")
            nxt[z] += 1
            renum += 1
        else:
            new_ids.append(None)
    changed = sum(1 for a, b in zip(g["id_orig"], new_ids) if (a if not is_null(a) else None) != b)
    g[idf] = new_ids
    log(f"  ID : {changed} identifiant(s) normalisé(s)/attribué(s) dont {renum} renumérotation(s) (original dans « id_orig »).")
    return g


def preprocess_layer(gdf: gpd.GeoDataFrame, layer: str, domains: dict, o: PreOpts, log):
    spec = LAYERS[layer]
    g = gdf.copy()
    rejected = []
    n0 = len(g)
    log(f"▶ {layer} ({n0} entités)")

    if o.clean_text:
        for f in spec.fields:
            if f.typ not in ("BOOLEAN", "DOUBLE", "INTEGER"):
                g[f.name] = g[f.name].map(clean_text)

    if o.fix_geometry:
        def fix(geom):
            if geom is None or geom.is_empty:
                return geom
            geom = shapely.force_2d(geom)
            if not geom.is_valid:
                geom = shapely.make_valid(geom)
            if geom.geom_type == "MultiPoint" and len(geom.geoms) == 1:
                geom = geom.geoms[0]
            if geom.geom_type == "MultiLineString":
                m = linemerge(geom)
                geom = m
            if geom.geom_type == "GeometryCollection":
                parts = [x for x in geom.geoms if x.geom_type == spec.geom]
                geom = parts[0] if len(parts) == 1 else (shapely.union_all(parts) if parts else geom)
            return geom
        g["geometry"] = g.geometry.apply(fix)
        g = g.set_crs(CRS_OUT, allow_override=True)
        multi = g.geom_type.isin(["MultiLineString", "MultiPoint"])
        if multi.any():
            log(f"  Géométrie : {int(multi.sum())} multi-partie(s) éclatée(s) en entités simples.")
            g = g.explode(index_parts=False).reset_index(drop=True)
    if o.fix_swapped_xy and spec.geom == "Point" and len(g):
        def inside(x, y):
            return BBOX[0] <= x <= BBOX[2] and BBOX[1] <= y <= BBOX[3]
        sw = 0
        pts = []
        for geom in g.geometry:
            if geom is not None and not geom.is_empty and geom.geom_type == "Point" and not inside(geom.x, geom.y) \
                    and inside(geom.y, geom.x):
                geom = shapely.Point(geom.y, geom.x)
                sw += 1
            pts.append(geom)
        if sw:
            g["geometry"] = gpd.GeoSeries(pts, index=g.index, crs=CRS_OUT)
            log(f"  Géométrie : {sw} point(s) avec lon/lat inversés corrigé(s).")
    if o.drop_empty_geom:
        bad = g.geometry.isna() | g.geometry.is_empty
        if bad.any():
            r = g[bad].copy()
            r["reject_reason"] = "Géométrie vide"
            rejected.append(r)
            g = g[~bad].copy()
            log(f"  Géométrie : {int(bad.sum())} entité(s) sans géométrie → rejets.")

    if o.coerce_types:
        for f in spec.fields:
            if f.typ == "BOOLEAN":
                g[f.name], _ = parse_bool(g[f.name])
            elif f.typ in ("DOUBLE", "INTEGER"):
                num, _ = parse_num(g[f.name])
                g[f.name] = num.round().astype("Int64") if f.typ == "INTEGER" else num

    if o.fix_domains:
        fixed = 0
        for f in spec.fields:
            vals = domains.get(layer, {}).get(f.dom or "", [])
            if not vals:
                continue
            nmap = domain_lookup(vals)

            def fx(v, f=f, vals=vals, nmap=nmap):
                if is_null(v):
                    return None
                toks = split_multi(v, set(nmap)) if f.typ == "MULTISELECT" else [str(v)]
                out = []
                for t in toks:
                    st, canon = match_domain(t, vals, nmap, cutoff=o.fuzzy_cutoff)
                    out.append(canon if st in ("ok", "case", "fuzzy", "free") else t)
                out = list(dict.fromkeys(out))
                return "; ".join(out) if f.typ == "MULTISELECT" else out[0]
            new = g[f.name].map(fx)
            fixed += int(((new != g[f.name]) & ~(new.isna() & g[f.name].isna())).sum())
            g[f.name] = new
        log(f"  Domaines : {fixed} valeur(s) normalisée(s) selon les dictionnaires.")

    if o.drop_exact_dupes and len(g):
        key = [f.name for f in spec.fields] + ["_wkb", "zone"]
        g["_wkb"] = g.geometry.to_wkb()
        d = g.duplicated(subset=key, keep="first")
        if d.any():
            r = g[d].drop(columns="_wkb").copy()
            r["reject_reason"] = "Doublon strict (mêmes attributs et géométrie)"
            rejected.append(r)
            log(f"  Doublons stricts : {int(d.sum())} supprimé(s) → rejets.")
        g = g[~d].drop(columns="_wkb")

    if o.harmonize_ids and len(g):
        g = harmonize_ids(g, layer, log)

    if o.add_coords and len(g):
        if spec.geom == "Point":
            g["lon"] = g.geometry.x.round(7)
            g["lat"] = g.geometry.y.round(7)
        else:
            try:
                g["longueur_m"] = g.to_crs(g.estimate_utm_crs()).length.round(1)
            except Exception:  # noqa
                pass
    g = g.reset_index(drop=True)
    rej = pd.concat(rejected) if rejected else None
    log(f"  → {len(g)} entités conservées, {0 if rej is None else len(rej)} rejetée(s).")
    return g, rej


def preprocess_all(layers: dict, domains: dict, o: PreOpts, log):
    out, rejected = {}, {}
    for name, g in layers.items():
        c, r = preprocess_layer(g, name, domains, o, log)
        c["_rid"] = range(len(c))   # nouvel identifiant interne après transformations
        out[name] = c
        if r is not None:
            rejected[name] = r
    return out, rejected


def quarantine(layers: dict, rejected: dict):
    """Déplace les entités en ERREUR vers « rejets »."""
    for name, g in layers.items():
        bad = g["qa_status"] == "ERROR"
        if bad.any():
            r = g[bad].copy()
            r["reject_reason"] = "Erreur QA: " + r["qa_flags"]
            rejected[name] = pd.concat([rejected[name], r]) if name in rejected else r
            layers[name] = g[~bad].reset_index(drop=True)
    return layers, rejected
