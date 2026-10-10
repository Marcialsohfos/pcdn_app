"""Orchestration : lecture -> reconnaissance de la couche -> fusion BRUTE par couche -> contrôle (marquage, sans suppression)."""
from __future__ import annotations

import geopandas as gpd
import pandas as pd

from .checks import qa_layer
from .readers import list_sources, read_source
from .standardize import META, column_lookup, detect_layer, rename_raw

ISSUE_COLS = ["layer", "severity", "type", "id", "nom", "agent", "date", "variable", "value", "message", "fichier", "ligne"]


def run_pipeline(settings: dict, schema: dict, log, progress=None) -> dict:
    src = list_sources(settings, log)
    log(f"Fichiers locaux à traiter : {len(src)}")
    flog, by_layer = [], {l: [] for l in schema["layers"]["layer"]}
    for k, row in enumerate(src.itertuples()):
        bn = row.path.name
        if progress:
            progress((k + 1) / max(1, len(src)) * 0.6)
        try:
            r = read_source(row.path, row.file_date)
        except Exception as e:
            log(str(e), level="ERROR")
            r = None
        if r is None:
            flog.append((bn, None, 0, "ERREUR", "Fichier illisible ou vide"))
            continue
        x, assumed = r
        layer = detect_layer(x, bn, schema, settings)
        if layer is None:
            cols = [c for c in x.columns if c not in META][:12]
            flog.append((bn, None, len(x), "NON RECONNU", "Colonnes : " + ", ".join(cols)))
            log(f"Couche non reconnue pour {bn}", level="WARN")
            continue
        h = rename_raw(x, layer, schema)
        variables = schema["vars"].loc[schema["vars"]["layer"] == layer, "variable"].tolist()
        missing = [v for v in variables if v not in h.columns]
        amb = sorted({c for c in column_lookup(schema, layer)["ambiguous"]} & {c.lower() for c in x.columns})
        det = [s for s in (f"colonnes absentes : {', '.join(missing)}" if missing else "",
                           f"colonnes ambiguës (noms tronqués) : {', '.join(amb)}" if amb else "",
                           "CRS absent : WGS84 supposé" if assumed else "") if s]
        flog.append((bn, layer, len(h), "OK (avertissements)" if det else "OK", " | ".join(det)))
        by_layer[layer].append(h)
    out = {"data": {}, "rows": {}, "issues": {},
           "file_log": pd.DataFrame(flog, columns=["fichier", "couche", "n_entites", "statut", "detail"])}
    todo = [l for l, v in by_layer.items() if v]
    for k, layer in enumerate(todo):
        if progress:
            progress(0.6 + 0.4 * (k + 1) / max(1, len(todo)))
        raw = gpd.GeoDataFrame(pd.concat(by_layer[layer], ignore_index=True), geometry="geometry", crs=4326)
        try:
            rows, iss = qa_layer(raw, layer, schema, settings, log)
        except Exception as e:
            log(f"Contrôle de {layer} en échec : {e} (les données brutes sont conservées)", level="ERROR")
            raw_rows = pd.DataFrame({"couche": layer, "fichier_source": raw["_source"], "ligne_source": raw["_row_src"],
                                     "statut": "Non contrôlé", "n_err": 0, "n_warn": 0, "problemes": "", "agent": "INCONNU",
                                     "date": None, "id": None, "nom": None, "format": raw["_fmt"], "groupe_doublon": None,
                                     "nature_doublon": "", "recommandation_doublon": "", "longitude": None, "latitude": None})
            rows, iss = raw_rows, pd.DataFrame(columns=ISSUE_COLS)
        out["data"][layer], out["rows"][layer], out["issues"][layer] = raw, rows, iss
        log(f"{layer:<32} {len(raw):5d} lignes brutes | {(iss.severity == 'error').sum():4d} erreurs | "
            f"{(iss.severity == 'warning').sum():4d} avertissements")
    frames = [v for v in out["issues"].values() if len(v)]
    out["issues_all"] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=ISSUE_COLS)
    return out
