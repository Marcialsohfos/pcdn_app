"""Reconnaissance de la couche, harmonisation des colonnes, validation des valeurs (listes, types, obligatoires)."""
from __future__ import annotations

import re
from collections import defaultdict

import geopandas as gpd
import numpy as np
import pandas as pd

from .util import clean_na, coalesce, norm_key, norm_val

META = {"geometry", "_source", "_file_date", "_multigeom"}


def trunc10(k) -> str:
    return norm_key(norm_key(k)[:10])


def column_lookup(schema: dict, layer: str) -> dict:
    """nom de colonne normalisé -> variable. Clés complètes (prioritaires) + clés tronquées à 10 car. (shapefile)."""
    v = schema["vars"][schema["vars"]["layer"] == layer]

    def table(fn):
        pairs = set()
        for _, r in v.iterrows():
            for src in (r["variable"], r["json_name"], r["label"]):
                k = fn(src)
                if k:
                    pairs.add((k, r["variable"]))
        by_key = defaultdict(set)
        for k, var in pairs:
            by_key[k].add(var)
        ok = {k: next(iter(s)) for k, s in by_key.items() if len(s) == 1}
        amb = {k for k, s in by_key.items() if len(s) > 1}
        return ok, amb

    full, _ = table(norm_key)
    tr, amb_tr = table(trunc10)
    return {"map": full, "trunc": tr, "ambiguous": amb_tr - set(full)}


def match_columns(cols, lk) -> list:
    out = []
    for c in cols:
        k = norm_key(c)
        m = lk["map"].get(k)
        if m is None and len(k) <= 10:
            m = lk["trunc"].get(k)
        out.append(m)
    return out


def detect_layer(x: gpd.GeoDataFrame, filename: str, schema: dict, settings: dict) -> str | None:
    cols = [c for c in x.columns if c not in META]
    fn = norm_key(filename.rsplit(".", 1)[0])
    gts = {g.upper().replace("MULTI", "") for g in x.geometry.geom_type.dropna().unique() if g != "GeometryCollection"}
    scores = []
    for _, r in schema["layers"].iterrows():
        l = r["layer"]
        n_match = sum(m is not None for m in match_columns(cols, column_lookup(schema, l)))
        name_hit = norm_key(l) in fn
        geom_ok = r["geometry"].upper() in gts
        scores.append((n_match + 100 * name_hit + 0.5 * geom_ok, l, n_match, name_hit, geom_ok))
    scores.sort(key=lambda t: -t[0])
    _, layer, n_match, name_hit, geom_ok = scores[0]
    return layer if (name_hit or (n_match >= settings["min_match_cols"] and geom_ok)) else None


def harmonize(x: gpd.GeoDataFrame, layer: str, schema: dict):
    """Renomme selon le dictionnaire, ajoute les colonnes manquantes -> (gdf, colonnes_absentes, colonnes_ambigues)."""
    v = schema["vars"][schema["vars"]["layer"] == layer].sort_values("ordre")
    lk = column_lookup(schema, layer)
    cols = [c for c in x.columns if c not in META]
    out: dict[str, pd.Series] = {}
    for c, m in zip(cols, match_columns(cols, lk)):
        name = m if m else "x_" + norm_key(c)
        s = clean_na(x[c])
        out[name] = coalesce(out[name], s) if name in out else s     # 2 colonnes -> même variable : 1re renseignée
    missing = [var for var in v["variable"] if var not in out]
    for var in missing:
        out[var] = pd.Series([None] * len(x), index=x.index, dtype="object")
    amb = sorted({norm_key(c) for c in cols} & lk["ambiguous"])
    df = pd.DataFrame(out, index=x.index)
    for m in ("_source", "_file_date", "_multigeom"):
        if m in x.columns:
            df[m] = x[m]
    return gpd.GeoDataFrame(df, geometry=x.geometry.values, crs=4326), missing, amb


def split_multiselect(s, dom_values: list[str]):
    """Découpe une réponse multi-choix avec les valeurs connues (certaines contiennent une virgule)."""
    if s is None:
        return [], []
    ns = f" {norm_val(s)} "
    nd = [norm_val(d) for d in dom_values]
    found = []
    for i in sorted(range(len(nd)), key=lambda i: -len(nd[i])):
        pat = f" {nd[i]} "
        if nd[i] and pat in ns:
            found.append(dom_values[i])
            ns = ns.replace(pat, " ", 1)
    rest = ns.strip()
    return [d for d in dom_values if d in found], ([rest] if rest else [])


BOOL_TRUE = {"oui", "true", "vrai", "1", "yes", "o", "y", "t"}
BOOL_FALSE = {"non", "false", "faux", "0", "no", "n", "f"}


class Issues:
    def __init__(self):
        self.rows: list[tuple] = []

    def add(self, rows, severity, type_, variable, value=None, message=""):
        rows = [int(r) for r in rows]
        if not rows:
            return
        n = len(rows)
        vals = list(value) if isinstance(value, (list, np.ndarray, pd.Series)) else [value] * n
        msgs = list(message) if isinstance(message, (list, np.ndarray)) else [message] * n
        for r, v, m in zip(rows, vals, msgs):
            self.rows.append((r, severity, type_, variable, None if v is None or (isinstance(v, float) and np.isnan(v)) else str(v), m))

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=["row", "severity", "type", "variable", "value", "message"])


def _num(a):
    try:
        f = float(re.sub(r"\s", "", a).replace(",", "."))
        return f if np.isfinite(f) else np.nan
    except Exception:
        return np.nan


def canonicalize(x: gpd.GeoDataFrame, layer: str, schema: dict):
    """Valide types / listes / obligatoires -> (données canonisées, Issues). `row` = position (0..n-1)."""
    v = schema["vars"][schema["vars"]["layer"] == layer].sort_values("ordre")
    dom = schema["domains"][schema["domains"]["layer"] == layer]
    iss, df = Issues(), x.copy()
    for _, r in v.iterrows():
        var, typ, lab = r["variable"], r["type"], r["label"]
        val = df[var].tolist()
        has = np.array([a is not None for a in val], dtype=bool)
        idx = np.arange(len(val))
        if r["obligatoire"]:
            iss.add(idx[~has], "error", "champ_obligatoire_vide", var, None,
                    f"Champ obligatoire « {lab} » ({var}) non renseigné")
        d = dom[dom["variable"] == var]
        if len(d) > 0 and typ in ("text", "multiselect"):
            dv = d.sort_values("ordre")["value"].tolist()
            if typ == "text":
                key = {}
                for a in dv:
                    key.setdefault(norm_val(a), a)
                hit = [key.get(norm_val(a)) if a is not None else None for a in val]
                bad = [i for i in idx if has[i] and hit[i] is None]
                iss.add(bad, "error", "valeur_hors_liste", var, [val[i] for i in bad],
                        [f"« {lab} » : valeur « {val[i]} » absente de la liste autorisée" for i in bad])
                val = [hit[i] if (has[i] and hit[i] is not None) else val[i] for i in idx]
            else:
                for i in idx[has]:
                    found, unknown = split_multiselect(val[i], dv)
                    if unknown:
                        iss.add([i], "error", "valeur_hors_liste", var, val[i],
                                f"« {lab} » : élément(s) hors liste « {' ; '.join(unknown)} »")
                    val[i] = "; ".join(found) if found else None
            df[var] = pd.Series(val, index=df.index, dtype="object")
        if typ in ("double", "integer"):
            num = np.array([_num(a) if a is not None else np.nan for a in val], dtype=float)
            bad = [i for i in idx if has[i] and np.isnan(num[i])]
            iss.add(bad, "error", "type_invalide", var, [val[i] for i in bad], [f"« {lab} » : « {val[i]} » n'est pas un nombre" for i in bad])
            if typ == "integer":
                ni = [i for i in idx if not np.isnan(num[i]) and num[i] != round(num[i])]
                iss.add(ni, "warning", "type_invalide", var, [val[i] for i in ni], [f"« {lab} » : entier attendu, reçu {val[i]}" for i in ni])
            lo, hi = pd.to_numeric(r.get("min"), errors="coerce"), pd.to_numeric(r.get("max"), errors="coerce")
            if pd.notna(lo):
                rr = [i for i in idx if not np.isnan(num[i]) and num[i] < lo]
                iss.add(rr, "error", "hors_plage", var, [num[i] for i in rr], [f"« {lab} » = {num[i]:g} inférieur au minimum plausible ({lo:g})" for i in rr])
            if pd.notna(hi):
                rr = [i for i in idx if not np.isnan(num[i]) and num[i] > hi]
                iss.add(rr, "error", "hors_plage", var, [num[i] for i in rr], [f"« {lab} » = {num[i]:g} supérieur au maximum plausible ({hi:g})" for i in rr])
            df[var] = num
        elif typ == "boolean":
            b = [True if norm_val(a) in BOOL_TRUE else (False if norm_val(a) in BOOL_FALSE else None) if a is not None else None for a in val]
            bad = [i for i in idx if has[i] and b[i] is None]
            iss.add(bad, "error", "type_invalide", var, [val[i] for i in bad], [f"« {lab} » : « {val[i]} » n'est pas Oui/Non" for i in bad])
            df[var] = pd.array(b, dtype="boolean")
    return df, iss
