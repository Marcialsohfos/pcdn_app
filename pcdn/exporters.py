"""Exports : données BRUTES par couche (GPKG, SHP, KML) + rapport de traitement (Excel) + corrections par agent + suivi quotidien."""
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

from .util import is_null

_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
INTERNAL = ("_fmt", "_file_date", "_multigeom")

ACTIONS = {  # type d'anomalie -> (équipe, action recommandée)
    "champ_obligatoire_vide": ("Terrain", "Renseigner le champ (retour à l'agent ou nouvelle visite)"),
    "valeur_hors_liste": ("Traitement", "Remplacer la valeur par l'une des valeurs de la liste Mapit"),
    "id_format": ("Traitement", "Renommer l'identifiant au format ZONE<n><2 lettres de la couche><n> (ex. ZONE4IN1)"),
    "id_duplique": ("Traitement", "Un même ID est utilisé pour des positions différentes : attribuer des ID uniques"),
    "doublon_exact": ("Traitement", "Copie identique : ne garder qu'une ligne après validation (aucune suppression automatique)"),
    "doublon_conflit": ("Traitement", "Mêmes ID/position mais valeurs différentes : choisir la bonne version avec l'agent"),
    "doublon_spatial": ("Traitement", "Deux points quasi confondus portant le même ID/nom : vérifier s'il s'agit du même objet"),
    "geometrie_vide": ("Terrain", "Position absente ou illisible : récupérer le point (GPS non capté) ou re-lever"),
    "geometrie_type": ("Traitement", "Type de géométrie inattendu pour cette couche : vérifier la couche d'origine"),
    "geometrie_invalide": ("Traitement", "Réparer la géométrie (auto-intersection, anneau non fermé)"),
    "hors_emprise": ("Traitement", "Position hors du corridor : coordonnées inversées ou erronées ? Corriger ou re-lever"),
    "hors_plage": ("Terrain", "Valeur numérique hors plage plausible : vérifier avec l'agent"),
    "type_invalide": ("Traitement", "Valeur de mauvais type (ex. texte à la place d'un nombre ou Oui/Non) : corriger"),
    "tronçon_court": ("Traitement", "Tronçon très court : vérifier qu'il ne s'agit pas d'un doublon ou d'un clic parasite"),
    "incoherence": ("Terrain", "Champs contradictoires entre eux : confirmer la bonne réponse avec l'agent"),
    "date_future": ("Traitement", "Date dans le futur (horloge du terminal mal réglée) : corriger la date"),
    "date_anterieure": ("Traitement", "Date antérieure au début de la collecte : vérifier le fichier"),
}


# ------------------------------------------------------------------ données brutes
def flat_raw(g: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Données brutes : valeurs inchangées. Seules sont ajoutées 2 colonnes de traçabilité (fichier_source, ligne_source)."""
    out = g[[c for c in g.columns if c not in INTERNAL]].copy()
    out = out.rename(columns={"_source": "fichier_source", "_row_src": "ligne_source"})
    out = out.rename(columns={c: "fid_source" for c in out.columns if str(c).lower() == "fid"})   # 'fid' est réservé par GeoPackage
    for c in out.columns:
        if c in ("geometry", "ligne_source"):
            continue
        out[c] = pd.Series([None if is_null(v) else _ILLEGAL.sub("", str(v)) for v in out[c].tolist()], index=out.index, dtype="object")
    out["ligne_source"] = out["ligne_source"].astype("int64")
    geom = out.geometry
    out["geometry"] = gpd.GeoSeries([None if (a is None or a.is_empty) else a for a in geom.values], index=out.index, crs=4326)
    return gpd.GeoDataFrame(out, geometry="geometry", crs=4326)


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


_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


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
            continue                                              # KML ne sait pas porter une ligne sans position (elle reste dans GPKG/SHP)
        label = r.get(id_var)
        label = "" if label is None or pd.isna(label) else label
        data = "".join(f'<Data name="{_x(c)}"><value>{_x(r[c])}</value></Data>' for c in cols if not pd.isna(r[c]) and str(r[c]) != "")
        parts = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
        gx = "".join(_kgeom(p) for p in parts)
        if len(parts) > 1:
            gx = f"<MultiGeometry>{gx}</MultiGeometry>"
        out.append(f"<Placemark><name>{_x(label)}</name><ExtendedData>{data}</ExtendedData>{gx}</Placemark>")
    return "".join(out) + "</Folder>"


def kml_doc(title: str, body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document><name>{_x(title)}</name>{body}</Document></kml>'


# ------------------------------------------------------------------ synthèse / rapport
def summarise_quality(res: dict) -> pd.DataFrame:
    iss, rows = res["issues_all"], res["rows"]
    out = []
    for layer, r in rows.items():
        sub = iss[iss["layer"] == layer]
        out.append({"couche": layer, "entites": len(r), "fichiers": r["fichier_source"].nunique(),
                    "conformes": int((r["statut"] == "Conforme").sum()), "a_verifier": int((r["statut"] == "À vérifier").sum()),
                    "a_corriger": int((r["statut"] == "À corriger").sum()),
                    "entites_en_doublon": int(r["groupe_doublon"].notna().sum()),
                    "erreurs": int((sub["severity"] == "error").sum()), "avertissements": int((sub["severity"] == "warning").sum()),
                    "entites_aujourdhui": int((r["date"] == date.today().isoformat()).sum())})
    q = pd.DataFrame(out, columns=["couche", "entites", "fichiers", "conformes", "a_verifier", "a_corriger", "entites_en_doublon",
                                   "erreurs", "avertissements", "entites_aujourdhui"])
    q["taux_conformite_pct"] = (100 * q["conformes"] / q["entites"].clip(lower=1)).round(1)
    return q.sort_values("entites", ascending=False).reset_index(drop=True)


def plan_action(iss: pd.DataFrame) -> pd.DataFrame:
    if not len(iss):
        return pd.DataFrame(columns=["priorité", "couche", "gravité", "type", "nb_lignes", "équipe", "action_recommandée"])
    g = iss.groupby(["layer", "severity", "type"]).agg(nb=("ligne", "size"), lignes=("ligne", "nunique")).reset_index()
    g["équipe"] = g["type"].map(lambda t: ACTIONS.get(t, ("", ""))[0])
    g["action_recommandée"] = g["type"].map(lambda t: ACTIONS.get(t, ("", "Vérifier"))[1])
    g["gravité"] = g["severity"].map({"error": "Erreur", "warning": "Avertissement"})
    g = g.sort_values(["severity", "nb"], ascending=[True, False]).reset_index(drop=True)
    g.insert(0, "priorité", np.arange(1, len(g) + 1))
    return g.rename(columns={"layer": "couche", "nb": "nb_anomalies", "lignes": "nb_lignes"})[
        ["priorité", "couche", "gravité", "type", "nb_anomalies", "nb_lignes", "équipe", "action_recommandée"]]


def coverage(rows: dict) -> pd.DataFrame:
    recs = []
    for layer, r in rows.items():
        for i in r["id"].tolist():
            m = re.match(r"^ZONE(\d+)", i, re.I) if isinstance(i, str) else None
            recs.append((layer, f"ZONE{int(m.group(1))}" if m else "SANS_ZONE"))
    if not recs:
        return pd.DataFrame()
    c = pd.crosstab(pd.Series([a for a, _ in recs], name="couche"), pd.Series([b for _, b in recs], name="zone"))
    cols = sorted([z for z in c.columns if z != "SANS_ZONE"], key=lambda z: int(z[4:])) + (["SANS_ZONE"] if "SANS_ZONE" in c.columns else [])
    return c[cols].reset_index()


def readme_sheet(q: pd.DataFrame, run_date: date) -> pd.DataFrame:
    tot, bad = int(q["entites"].sum()), int(q["a_corriger"].sum())
    lines = [
        f"RAPPORT DE TRAITEMENT PCDN – généré le {run_date.isoformat()}",
        "",
        f"{tot} lignes brutes consolidées dans {len(q)} couche(s) ; {bad} ligne(s) à corriger.",
        "",
        "PRINCIPE : aucune donnée n'est supprimée ni modifiée. Les exports (GPKG, SHP, KML) contiennent toutes les lignes reçues,",
        "telles que collectées. Les problèmes sont seulement signalés dans ce rapport ; chaque ligne se retrouve grâce à",
        "'fichier_source' + 'ligne_source' (colonnes de traçabilité ajoutées aux exports).",
        "",
        "COMMENT TRAVAILLER",
        "1. Plan_action : commencer par le haut (erreurs les plus nombreuses). La colonne 'équipe' dit qui agit.",
        "2. A_traiter : la liste ligne par ligne (statut À corriger / À vérifier) avec les problèmes détectés.",
        "3. Doublons : groupes d'enregistrements présents plusieurs fois. La colonne 'recommandation' propose quelle ligne conserver ;",
        "   la décision finale reste humaine.",
        "4. Champs_vides : champs obligatoires les plus souvent non renseignés (retour de formation aux agents).",
        "5. Couverture_zones : nombre d'entités par couche et par zone (d'après l'ID ZONE<n>...). Un 0 = zone non encore couverte.",
        "6. corrections_par_agent/ : un classeur par agent à lui renvoyer.",
        "",
        "STATUTS : Conforme = aucun problème · À vérifier = avertissement seulement · À corriger = au moins une erreur.",
    ]
    return pd.DataFrame({"Guide": lines})


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
                ws.column_dimensions[ws.cell(1, j).column_letter].width = min(70, max(10, width))


def build_report(res: dict, schema: dict, path: Path, run_date: date):
    q, iss = summarise_quality(res), res["issues_all"]
    rows = pd.concat(res["rows"].values(), ignore_index=True)
    a_traiter = rows[rows["statut"].isin(["À corriger", "À vérifier"])].copy()
    a_traiter["_o"] = (a_traiter["statut"] != "À corriger")
    a_traiter = a_traiter.sort_values(["_o", "couche", "agent", "fichier_source", "ligne_source"]).drop(columns="_o")
    a_traiter = a_traiter[["couche", "statut", "fichier_source", "ligne_source", "id", "nom", "agent", "date", "n_err", "n_warn", "problemes",
                           "recommandation_doublon"]].rename(columns={"n_err": "nb_erreurs", "n_warn": "nb_avertissements",
                                                                     "problemes": "problèmes_détectés", "recommandation_doublon": "recommandation_doublon"})
    dbl = rows[rows["groupe_doublon"].notna()].copy()
    dbl["groupe"] = dbl["couche"] + "-" + dbl["groupe_doublon"].astype(int).astype(str)
    dbl = dbl.sort_values(["couche", "groupe_doublon", "fichier_source", "ligne_source"])[
        ["groupe", "couche", "nature_doublon", "fichier_source", "ligne_source", "format", "id", "nom", "agent", "recommandation_doublon"]]
    vides = pd.DataFrame(columns=["couche", "champ", "nb_lignes_vides", "pct_des_lignes"])
    if len(iss):
        v = iss[iss["type"] == "champ_obligatoire_vide"].groupby(["layer", "variable"]).size().reset_index(name="nb_lignes_vides")
        v["pct_des_lignes"] = (100 * v["nb_lignes_vides"] / v["layer"].map(q.set_index("couche")["entites"])).round(1)
        vides = v.sort_values("nb_lignes_vides", ascending=False).rename(columns={"layer": "couche", "variable": "champ"})
    by_agent = pd.DataFrame(columns=["agent", "couche", "lignes", "erreurs", "avertissements"])
    if len(iss):
        by_agent = (rows.groupby(["agent", "couche"]).agg(lignes=("id", "size"), lignes_a_corriger=("n_err", lambda s: int((s > 0).sum())))
                    .reset_index().sort_values("lignes_a_corriger", ascending=False))
    cov = coverage(res["rows"])
    anomalies = iss.rename(columns={"layer": "couche", "severity": "gravité", "fichier": "fichier_source", "ligne": "ligne_source"})
    _write_sheets(path, {"Lisez-moi": readme_sheet(q, run_date), "Synthèse": q, "Plan_action": plan_action(iss), "A_traiter": a_traiter,
                         "Doublons": dbl, "Champs_vides": vides, "Par_agent": by_agent, "Couverture_zones": cov,
                         "Anomalies": anomalies, "Journal_fichiers": res["file_log"]})
    return q


# ------------------------------------------------------------------ export principal
def export_all(res: dict, schema: dict, settings: dict, log, run_date: date | None = None, formats=("gpkg", "shp", "kml")):
    run_date = run_date or date.today()
    latest = Path(settings["dir_output"]) / "latest"
    arch = Path(settings["dir_output"]) / "archive" / run_date.isoformat()
    shutil.rmtree(latest, ignore_errors=True)
    for d in (latest, arch):
        d.mkdir(parents=True, exist_ok=True)
    if not res["data"]:
        log("Aucune donnée à exporter", level="WARN")
        return None
    raw_dir, rep_dir = latest / "donnees_brutes", latest / "rapport"
    raw_dir.mkdir()
    rep_dir.mkdir()
    flat = {l: flat_raw(g) for l, g in res["data"].items()}
    ids = schema["layers"].set_index("layer")["id_var"].to_dict()
    if "gpkg" in formats:
        for l, g in flat.items():
            try:
                g.to_file(raw_dir / "pcdn_brut.gpkg", layer=l, driver="GPKG", engine="pyogrio")
            except Exception as e:
                log(f"GPKG {l} : {e}", level="ERROR")
    if "shp" in formats:
        (raw_dir / "shp").mkdir()
        for l, g in flat.items():
            m = shp_names([c for c in g.columns if c != "geometry"])
            pd.DataFrame({"champ_complet": list(m), "champ_shp": list(m.values())}).to_csv(raw_dir / "shp" / f"{l}_champs.csv", index=False, encoding="utf-8-sig")
            try:
                g.rename(columns=m).to_file(raw_dir / "shp" / f"{l}.shp", driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
            except Exception as e:
                log(f"SHP {l} : {e}", level="ERROR")
    if "kml" in formats:
        (raw_dir / "kml").mkdir()
        folders = {l: kml_folder(g, l, ids[l]) for l, g in flat.items()}
        for l, body in folders.items():
            (raw_dir / "kml" / f"{l}.kml").write_text(kml_doc(l, body), encoding="utf-8")
        (raw_dir / "kml" / "PCDN_toutes_couches.kml").write_text(kml_doc("PCDN", "".join(folders.values())), encoding="utf-8")

    q = build_report(res, schema, rep_dir / "rapport_traitement.xlsx", run_date)
    ag_dir = rep_dir / "corrections_par_agent"
    ag_dir.mkdir()
    iss = res["issues_all"].rename(columns={"layer": "couche", "severity": "gravité", "fichier": "fichier_source", "ligne": "ligne_source"})
    for a in iss["agent"].dropna().unique():
        _write_sheets(ag_dir / (re.sub(r"[^A-Za-z0-9_-]+", "_", str(a)) + ".xlsx"), {"Corrections": iss[iss["agent"] == a]})

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
