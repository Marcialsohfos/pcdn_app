"""Exports : GeoPackage, Excel (données + qualité + corrections par agent), suivi quotidien, SHP/KML optionnels."""
from __future__ import annotations

import io
import re
import shutil
import zipfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

import geopandas as gpd
import numpy as np
import pandas as pd

RENAME = {"_date": "date_collecte", "_agent": "agent", "_file_date": "date_fichier", "_source": "fichier_source",
          "_n_err": "nb_erreurs", "_n_warn": "nb_avertissements"}
_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def flat_for_export(g: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Colonnes internes retirées/renommées, booléens -> Oui/Non, dates -> texte."""
    keep = [c for c in g.columns if not (c.startswith("x_") or c == "_multigeom")]
    out = g[keep].copy()
    for c in out.columns:
        if c == "geometry":
            continue
        if str(out[c].dtype) == "boolean":
            out[c] = pd.Series([None if pd.isna(v) else ("Oui" if v else "Non") for v in out[c]], index=out.index, dtype="object")
        elif pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
    return out.rename(columns=RENAME)


def summarise_quality(res: dict) -> pd.DataFrame:
    iss = res["issues_all"]
    rows = []
    for layer, d in res["data"].items():
        sub = iss[iss["layer"] == layer]
        rows.append({"couche": layer, "entites": len(d), "erreurs": int((sub.severity == "error").sum()),
                     "avertissements": int((sub.severity == "warning").sum()),
                     "entites_avec_erreur": int((d["_n_err"] > 0).sum()),
                     "entites_aujourdhui": int((d["_date"] == pd.Timestamp.today().normalize()).sum())})
    q = pd.DataFrame(rows, columns=["couche", "entites", "erreurs", "avertissements", "entites_avec_erreur", "entites_aujourdhui"])
    q["taux_conformite_pct"] = (100 * (1 - q["entites_avec_erreur"] / q["entites"].clip(lower=1))).round(1)
    return q.sort_values("entites", ascending=False).reset_index(drop=True)


def _clean_xl(df: pd.DataFrame) -> pd.DataFrame:
    df = pd.DataFrame(df).copy()
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = df[c].map(lambda v: _ILLEGAL.sub("", v) if isinstance(v, str) else v)
    return df


def _write_sheets(path: Path, sheets: dict[str, pd.DataFrame]):
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        for name, df in sheets.items():
            df = _clean_xl(df)
            df.to_excel(w, sheet_name=name[:31], index=False)
            ws = w.sheets[name[:31]]
            ws.freeze_panes = "A2"
            for j, col in enumerate(df.columns, 1):
                width = max([len(str(col))] + [len(str(v)) for v in df[col].head(200).tolist()]) + 2
                ws.column_dimensions[ws.cell(1, j).column_letter].width = min(60, max(10, width))


# ---- SHP (noms ≤ 10 car.) ------------------------------------------------------------------
def shp_names(cols) -> dict[str, str]:
    used, m = set(), {}
    for c in cols:
        s, i = c[:10], 0
        while s.lower() in used:
            i += 1
            s = f"{c[:10 - len(str(i)) - 1]}_{i}"
        used.add(s.lower())
        m[c] = s
    return m


# ---- KML (écrit à la main : ExtendedData lisible par Mapit/QGIS/Google Earth) -----------------
_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
STATUS = {"OK": "ff50b45a", "WARNING": "ff1e9bf5", "ERROR": "ff3c3cdc"}


def _x(v) -> str:
    return escape(_BAD.sub("", str(v)))


def _cs(seq) -> str:
    return " ".join(f"{p[0]:.7f},{p[1]:.7f},0" for p in seq)


def _kgeom(p) -> str:
    if p.geom_type == "Point":
        return f"<Point><coordinates>{p.x:.7f},{p.y:.7f},0</coordinates></Point>"
    if p.geom_type == "Polygon":
        inner = "".join(f"<innerBoundaryIs><LinearRing><coordinates>{_cs(r.coords)}</coordinates></LinearRing></innerBoundaryIs>" for r in p.interiors)
        return f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{_cs(p.exterior.coords)}</coordinates></LinearRing></outerBoundaryIs>{inner}</Polygon>"
    return f"<LineString><tessellate>1</tessellate><coordinates>{_cs(p.coords)}</coordinates></LineString>"


def kml_folder(g: gpd.GeoDataFrame, layer: str, id_var: str) -> str:
    cols = [c for c in g.columns if c != "geometry"]
    out = [f"<Folder><name>{_x(layer)}</name>"]
    for _, r in g.iterrows():
        geom = r.geometry
        if geom is None or geom.is_empty:
            continue
        nerr, nwarn = r.get("nb_erreurs", 0), r.get("nb_avertissements", 0)
        status = "ERROR" if nerr and nerr > 0 else ("WARNING" if nwarn and nwarn > 0 else "OK")
        label = r.get(id_var)
        label = "" if label is None or pd.isna(label) else label
        data = "".join(f'<Data name="{_x(c)}"><value>{_x(r[c])}</value></Data>' for c in cols if not pd.isna(r[c]) and str(r[c]) != "")
        parts = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
        gx = "".join(_kgeom(p) for p in parts)
        if len(parts) > 1:
            gx = f"<MultiGeometry>{gx}</MultiGeometry>"
        out.append(f"<Placemark><name>{_x(label)}</name><styleUrl>#{status}</styleUrl><ExtendedData>{data}</ExtendedData>{gx}</Placemark>")
    return "".join(out) + "</Folder>"


def kml_doc(title: str, body: str) -> str:
    st = "".join(f'<Style id="{k}"><IconStyle><color>{c}</color></IconStyle><LineStyle><color>{c}</color><width>3</width></LineStyle></Style>'
                 for k, c in STATUS.items())
    return f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>{_x(title)}</name>{st}{body}</Document></kml>'


# ---- export principal ------------------------------------------------------------------------
def export_all(res: dict, schema: dict, settings: dict, log, run_date: date | None = None, formats=("gpkg", "xlsx")) -> pd.DataFrame | None:
    run_date = run_date or date.today()
    latest = Path(settings["dir_output"]) / "latest"
    arch = Path(settings["dir_output"]) / "archive" / run_date.isoformat()
    shutil.rmtree(latest, ignore_errors=True)
    for d in (latest, arch):
        d.mkdir(parents=True, exist_ok=True)
    if not res["data"]:
        log("Aucune donnée à exporter", level="WARN")
        return None
    flat = {l: flat_for_export(d) for l, d in res["data"].items()}
    ids = schema["layers"].set_index("layer")["id_var"].to_dict()

    gpkg = latest / "pcdn_donnees.gpkg"
    for l, g in flat.items():
        g.to_file(gpkg, layer=l, driver="GPKG", engine="pyogrio")

    sheets = {}
    for l, g in flat.items():
        t = pd.DataFrame(g.drop(columns="geometry"))
        cen = g.geometry.centroid
        t["longitude"], t["latitude"] = cen.x.round(6).to_numpy(), cen.y.round(6).to_numpy()
        sheets[l] = t
    _write_sheets(latest / "pcdn_donnees.xlsx", sheets)

    q, iss = summarise_quality(res), res["issues_all"].copy()
    by_agent = (iss.groupby(["agent", "layer"]).agg(erreurs=("severity", lambda s: (s == "error").sum()),
                avertissements=("severity", lambda s: (s == "warning").sum())).reset_index()
                .rename(columns={"layer": "couche"}).sort_values("erreurs", ascending=False)) if len(iss) else pd.DataFrame(columns=["agent", "couche", "erreurs", "avertissements"])
    by_type = (iss.groupby(["layer", "severity", "type"]).size().reset_index(name="nombre").sort_values("nombre", ascending=False)
               if len(iss) else pd.DataFrame(columns=["layer", "severity", "type", "nombre"]))
    _write_sheets(latest / "controle_qualite.xlsx", {"Resume": q, "Par_type_anomalie": by_type, "Par_agent": by_agent,
                                                      "Anomalies": iss, "Journal_fichiers": res["file_log"]})
    ag_dir = latest / "corrections_par_agent"
    ag_dir.mkdir()
    for a in iss["agent"].dropna().unique():
        _write_sheets(ag_dir / (re.sub(r"[^A-Za-z0-9_-]+", "_", str(a)) + ".xlsx"), {"Corrections": iss[iss["agent"] == a]})

    if "shp" in formats:
        (latest / "shp").mkdir()
        for l, g in flat.items():
            m = shp_names([c for c in g.columns if c != "geometry"])
            gs = g.rename(columns=m)
            pd.DataFrame({"champ_complet": list(m), "champ_shp": list(m.values())}).to_csv(latest / "shp" / f"{l}_champs.csv", index=False, encoding="utf-8-sig")
            gs.to_file(latest / "shp" / f"{l}.shp", driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
    if "kml" in formats:
        (latest / "kml").mkdir()
        folders = {l: kml_folder(g, l, ids[l]) for l, g in flat.items()}
        for l, body in folders.items():
            (latest / "kml" / f"{l}.kml").write_text(kml_doc(l, body), encoding="utf-8")
        (latest / "kml" / "PCDN_toutes_couches.kml").write_text(kml_doc("PCDN", "".join(folders.values())), encoding="utf-8")

    # suivi quotidien (historique cumulé : une ligne par jour et par couche)
    hp = Path(settings["dir_output"]) / "suivi_quotidien.csv"
    today = q[["couche", "entites", "entites_aujourdhui", "erreurs", "avertissements", "taux_conformite_pct"]].copy()
    today.insert(0, "date_execution", run_date.isoformat())
    hist = pd.read_csv(hp, dtype={"date_execution": str}) if hp.exists() else today.iloc[0:0]
    hist = pd.concat([hist[hist["date_execution"] != run_date.isoformat()], today], ignore_index=True).sort_values(["date_execution", "couche"])
    hist.to_csv(hp, index=False, encoding="utf-8")

    shutil.copytree(latest, arch, dirs_exist_ok=True)
    log(f"Exports écrits dans {latest} (archive : {arch})")
    return q


def zip_latest(settings: dict) -> bytes:
    latest = Path(settings["dir_output"]) / "latest"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(latest.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(latest))
        hp = Path(settings["dir_output"]) / "suivi_quotidien.csv"
        if hp.exists():
            z.write(hp, "suivi_quotidien.csv")
    return buf.getvalue()
