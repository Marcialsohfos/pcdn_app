"""PCDN Corridor — Collecte, fusion, assurance qualité, pré-traitement et export des couches terrain."""
from __future__ import annotations

import tempfile
import uuid
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st

from pcdn import exporters, qa, readers
from pcdn.config import LAYERS, check_spec, load_domains
from pcdn.preprocess import PreOpts, preprocess_all, quarantine
from pcdn.sources import FtpClient, SrcFile, build_sources, fetch

st.set_page_config(page_title="PCDN · Pipeline cartographique", page_icon="🚆", layout="wide")
DOMAINS = load_domains()
SS = st.session_state
for k, v in {"srcs": None, "root": None, "raw": None, "struct": None, "issues_raw": None, "clean": None,
             "rejected": {}, "issues_clean": None, "exports": {}, "log": [], "bundle": None, "ftp_cfg": None}.items():
    SS.setdefault(k, v)


def log(msg: str):
    SS.log.append(msg)


def secret(key, default=""):
    try:
        return st.secrets["ftp"].get(key, default)
    except Exception:  # noqa
        return default


# ================================================================== SIDEBAR
with st.sidebar:
    st.title("🚆 PCDN Corridor")
    st.caption("Pipeline : FTP → fusion → QA → pré-traitement → export")
    probs = check_spec(DOMAINS)
    if probs:
        st.error("Dictionnaires JSON incohérents :\n\n" + "\n".join(f"- {p}" for p in probs[:6]))
    dup_tol = st.number_input("Seuil de doublon de points (m)", 0.5, 100.0, 3.0, 0.5)
    st.divider()
    st.markdown("**Convention d'ID** : `ZONE<n>` + 2 premières lettres de la couche + n°")
    st.dataframe(pd.DataFrame({"Couche": list(LAYERS), "Préfixe": [l.prefix for l in LAYERS.values()],
                               "Exemple": [f"ZONE1{l.prefix}1" for l in LAYERS.values()]}),
                 hide_index=True, width="stretch")
    if st.button("♻ Réinitialiser la session"):
        st.session_state.clear()
        st.rerun()

tabs = st.tabs(["1 · Source", "2 · Fusion", "3 · Qualité", "4 · Pré-traitement", "5 · Export"])


# ================================================================== 1. SOURCE
with tabs[0]:
    mode = st.radio("Origine des données", ["Serveur FTP", "Téléversement manuel (secours)"], horizontal=True)
    workdir = Path(tempfile.gettempdir()) / "pcdn_work"
    ftp = None

    if mode == "Serveur FTP":
        c1, c2, c3, c4 = st.columns([3, 1, 2, 2])
        host = c1.text_input("Hôte", secret("host"))
        port = c2.number_input("Port", 1, 65535, int(secret("port", 21)))
        user = c3.text_input("Utilisateur", secret("user"))
        pwd = c4.text_input("Mot de passe", secret("password"), type="password")
        c5, c6, c7, c8 = st.columns([3, 1, 2, 2])
        root = c5.text_input("Dossier racine", secret("root", "/"))
        depth = c6.number_input("Profondeur", 1, 10, 5)
        tls = c7.checkbox("Utiliser FTPS (chiffré)", True)
        plain = c8.checkbox("Autoriser repli FTP non chiffré", False,
                            help="À éviter : identifiants et données circulent en clair.")
        if not secret("host"):
            st.info("Astuce : placez les identifiants dans `.streamlit/secrets.toml` (voir `secrets.toml.example`) "
                    "plutôt que de les saisir ou de les écrire dans le code.")
        if st.button("🔎 Scanner le serveur", type="primary", disabled=not (host and user)):
            try:
                with st.spinner("Connexion et exploration…"), FtpClient(host, user, pwd, port, tls, plain) as cl:
                    entries = cl.walk(root, depth)
                    SS.ftp_cfg = dict(host=host, user=user, pwd=pwd, port=port, tls=tls, allow_plain=plain)
                    st.success(f"Connecté ({cl.mode}) — {len(entries)} fichiers vus.")
                SS.srcs, SS.root = build_sources(entries), None
            except Exception as e:  # noqa
                st.error(f"Échec : {e}")
    else:
        ups = st.file_uploader("Shapefiles (.shp + .shx + .dbf + .prj + .cpg), KML, KMZ ou ZIP", accept_multiple_files=True,
                               type=["shp", "shx", "dbf", "prj", "cpg", "kml", "kmz", "zip"])
        if st.button("📂 Analyser les fichiers", type="primary", disabled=not ups):
            root = workdir / f"up_{uuid.uuid4().hex[:8]}"
            root.mkdir(parents=True, exist_ok=True)
            entries = []
            for u in ups:
                (root / u.name).write_bytes(u.getbuffer())
                entries.append((u.name, u.size))
            SS.srcs, SS.root, SS.ftp_cfg = build_sources(entries), root, None

    if SS.srcs is not None:
        st.subheader("Fichiers détectés")
        st.caption("Vérifiez / corrigez la couche et la zone détectées (depuis le nom ou le chemin), décochez ce qui est à ignorer.")
        df = pd.DataFrame([{"inclure": s.include, "couche": s.layer, "zone": s.zone, "format": s.fmt,
                            "taille_ko": round(s.size / 1024, 1), "chemin": s.path} for s in SS.srcs])
        if df.empty:
            st.warning("Aucun fichier .shp / .kml / .kmz / .zip trouvé.")
        else:
            ed = st.data_editor(
                df, hide_index=True, width="stretch", disabled=["format", "taille_ko", "chemin"],
                column_config={"couche": st.column_config.SelectboxColumn(options=list(LAYERS)),
                               "zone": st.column_config.TextColumn(help="Ex. ZONE1")})
            miss = ed[ed.inclure & (ed.couche.isna() | ed.zone.isna() | (ed.zone == ""))]
            if len(miss):
                st.warning(f"{len(miss)} fichier(s) sans couche ou zone : la zone sera déduite des ID si possible.")
            if st.button("⬇ Récupérer et lire les données", type="primary"):
                for s, (_, r) in zip(SS.srcs, ed.iterrows()):
                    s.include, s.layer = bool(r.inclure), r.couche or None
                    s.zone = (str(r.zone).upper().replace(" ", "") if r.zone else None)
                SS.log = []
                resolved, bar = [], st.progress(0.0, "Téléchargement…")
                todo = [s for s in SS.srcs if s.include and s.layer]
                try:
                    cl = FtpClient(**{**SS.ftp_cfg, "pwd": SS.ftp_cfg["pwd"]}).connect() if SS.ftp_cfg else None
                    for i, s in enumerate(todo, 1):
                        bar.progress(i / max(len(todo), 1), f"{s.path}")
                        try:
                            resolved += fetch(s, workdir / "dl", cl, SS.root)
                        except Exception as e:  # noqa
                            log(f"❌ {s.path}: {e}")
                    if cl:
                        cl.close()
                    SS.raw, SS.struct = readers.ingest(resolved, log)
                    SS.issues_raw = qa.run_all_qa(SS.raw, DOMAINS, SS.struct, dup_tol)
                    SS.clean = SS.issues_clean = SS.bundle = None
                    bar.empty()
                    st.success(f"{sum(len(g) for g in SS.raw.values())} entités lues dans {len(SS.raw)} couche(s). "
                               "Passez à l'onglet « Fusion ».")
                except Exception as e:  # noqa
                    bar.empty()
                    st.error(f"Erreur : {e}")
    if SS.log:
        with st.expander("Journal", expanded=False):
            st.code("\n".join(SS.log))


# ================================================================== helpers
def current():
    """Jeu de données courant : nettoyé si disponible, sinon brut."""
    return (SS.clean, SS.issues_clean, "pré-traité") if SS.clean is not None else (SS.raw, SS.issues_raw, "brut")


def show_map(layers: dict, key: str):
    name = st.selectbox("Couche à afficher", list(layers), key=key)
    g = layers[name]
    g = g[g.geometry.notna() & ~g.geometry.is_empty].copy()
    if g.empty:
        st.info("Aucune géométrie.")
        return
    col = {"OK": [60, 180, 90, 200], "WARNING": [245, 155, 30, 220], "ERROR": [220, 60, 60, 230]}
    status = g["qa_status"] if "qa_status" in g else pd.Series("OK", index=g.index)
    g["color"] = status.map(col).map(lambda c: c or [120, 120, 120, 200])
    keep = [LAYERS[name].id_field, "zone", "color", "geometry"] + (["qa_status"] if "qa_status" in g else [])
    layer = pdk.Layer("GeoJsonLayer", data=g[keep].__geo_interface__, get_fill_color="properties.color",
                      get_line_color="properties.color", point_radius_min_pixels=6, line_width_min_pixels=3, pickable=True)
    xmin, ymin, xmax, ymax = g.total_bounds
    view = pdk.ViewState(latitude=(ymin + ymax) / 2, longitude=(xmin + xmax) / 2, zoom=6.5 if xmax - xmin > 1 else 10)
    st.pydeck_chart(pdk.Deck(layers=[layer], initial_view_state=view, map_style=None,
                             tooltip={"text": "{" + LAYERS[name].id_field + "}\n{zone}"}))


# ================================================================== 2. FUSION
with tabs[1]:
    if SS.raw is None:
        st.info("Récupérez d'abord les données (onglet 1).")
    else:
        data, _, label = current()
        st.subheader(f"Couches fusionnées ({label})")
        zones = sorted({z for g in data.values() for z in g["zone"].dropna().unique()})
        cov = pd.DataFrame({z: {n: int((g["zone"] == z).sum()) for n, g in data.items()} for z in zones})
        cov.insert(0, "Total", [len(g) for g in data.values()])
        exp = st.text_input("Zones attendues (facultatif, ex. ZONE1,ZONE2,ZONE3,ZONE4)", "")
        for z in [x.strip().upper() for x in exp.split(",") if x.strip()]:
            if z not in cov:
                cov[z] = 0
        st.caption("Matrice de couverture couche × zone — les 0 signalent des levés manquants.")
        st.dataframe(cov.style.map(lambda v: "background-color:#fde2e2" if v == 0 else "", subset=[c for c in cov if c != "Total"]),
                     width="stretch")
        missing_layers = [n for n in LAYERS if n not in data]
        if missing_layers:
            st.warning("Couches non reçues : " + ", ".join(missing_layers))
        show_map(data, "map_fusion")


# ================================================================== 3. QUALITÉ
with tabs[2]:
    if SS.raw is None:
        st.info("Récupérez d'abord les données (onglet 1).")
    else:
        data, issues, label = current()
        if st.button("🔄 Relancer le contrôle qualité"):
            issues = qa.run_all_qa(data, DOMAINS, SS.struct if label == "brut" else None, dup_tol)
            if label == "brut":
                SS.issues_raw = issues
            else:
                SS.issues_clean = issues
            st.rerun()
        st.subheader(f"Assurance qualité — jeu {label}")
        summ = qa.summarize(data, issues)
        tot = int(summ["Entités"].sum())
        nerr = int(summ["Erreurs (entités)"].sum())
        nwar = int(summ["Avertissements seuls (entités)"].sum())
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Entités", tot)
        m2.metric("En erreur", nerr)
        m3.metric("Avertissements seuls", nwar)
        m4.metric("Conformes", f"{100 * (tot - nerr - nwar) / tot:.1f} %" if tot else "—")
        st.dataframe(summ, hide_index=True, width="stretch")

        st.markdown("#### Explorateur d'anomalies")
        f1, f2, f3, f4 = st.columns(4)
        sel_l = f1.multiselect("Couche", sorted(issues.layer.unique()))
        sel_s = f2.multiselect("Sévérité", ["ERROR", "WARNING", "INFO"], ["ERROR", "WARNING"])
        sel_r = f3.multiselect("Règle", sorted(issues.rule.unique()))
        sel_z = f4.multiselect("Zone", sorted(issues.zone.dropna().unique()))
        v = issues.copy()
        if sel_l:
            v = v[v.layer.isin(sel_l)]
        if sel_s:
            v = v[v.severity.isin(sel_s)]
        if sel_r:
            v = v[v.rule.isin(sel_r)]
        if sel_z:
            v = v[v.zone.isin(sel_z)]
        st.caption(f"{len(v)} anomalie(s) affichée(s)")
        st.dataframe(v.drop(columns="_rid").assign(severity=v.severity.astype(str)), hide_index=True, width="stretch")
        c1, c2 = st.columns(2)
        c1.download_button("⬇ Anomalies (CSV)", v.assign(severity=v.severity.astype(str)).to_csv(index=False, encoding="utf-8-sig"),
                           "anomalies_QA.csv", "text/csv")
        top = issues[issues.severity != "INFO"].groupby("rule").size().sort_values(ascending=False).head(15)
        if len(top):
            c2.bar_chart(top, horizontal=True)
        with st.expander("Complétude des champs (% renseignés)"):
            comp = qa.completeness(data)
            st.dataframe(comp.pivot(index="Champ", columns="Couche", values="Renseigné (%)").dropna(how="all"),
                         width="stretch")


# ================================================================== 4. PRÉ-TRAITEMENT
with tabs[3]:
    if SS.raw is None:
        st.info("Récupérez d'abord les données (onglet 1).")
    else:
        st.subheader("Options de pré-traitement")
        c1, c2, c3 = st.columns(3)
        o = PreOpts(
            clean_text=c1.checkbox("Nettoyer les textes (espaces, vides, caractères parasites)", True),
            fix_geometry=c1.checkbox("Réparer les géométries (2D, make_valid, multi→simple)", True),
            fix_swapped_xy=c1.checkbox("Corriger lon/lat inversés", True),
            drop_empty_geom=c1.checkbox("Écarter les entités sans géométrie", True),
            coerce_types=c2.checkbox("Typer booléens / nombres", True),
            fix_domains=c2.checkbox("Normaliser selon les dictionnaires (casse, accents, tirets)", True),
            fuzzy_cutoff=c2.slider("Tolérance fautes de frappe (0 = off)", 0.0, 1.0, 0.88, 0.01),
            drop_exact_dupes=c3.checkbox("Supprimer les doublons stricts", True),
            harmonize_ids=c3.checkbox("Harmoniser les ID (ZONE<n>XX<n>)", True),
            add_coords=c3.checkbox("Ajouter lon/lat (points) ou longueur_m (lignes)", True),
            quarantine_errors=c3.checkbox("Mettre en quarantaine les entités encore en ERREUR", False,
                                          help="Elles sont retirées des exports principaux et conservées dans « rejets »."),
        )
        if st.button("⚙ Lancer le pré-traitement", type="primary"):
            SS.log = []
            with st.spinner("Traitement…"):
                clean, rej = preprocess_all(SS.raw, DOMAINS, o, log)
                SS.issues_clean = qa.run_all_qa(clean, DOMAINS, None, dup_tol)
                clean = qa.apply_status(clean, SS.issues_clean)
                if o.quarantine_errors:
                    clean, rej = quarantine(clean, rej)
                SS.clean, SS.rejected, SS.bundle = clean, rej, None
            st.success("Pré-traitement terminé — le jeu pré-traité est désormais celui utilisé dans les onglets Qualité et Export.")
        if SS.clean is not None:
            b, a = qa.summarize(SS.raw, SS.issues_raw), qa.summarize(SS.clean, SS.issues_clean)
            cmp_ = b.merge(a, on="Couche", how="outer", suffixes=(" avant", " après"))
            st.markdown("#### Avant / après")
            st.dataframe(cmp_[["Couche", "Entités avant", "Entités après", "Erreurs (entités) avant", "Erreurs (entités) après",
                               "Entités OK (%) avant", "Entités OK (%) après"]], hide_index=True, width="stretch")
            if SS.rejected:
                with st.expander(f"Rejets ({sum(len(r) for r in SS.rejected.values())})"):
                    for n, r in SS.rejected.items():
                        st.write(f"**{n}**")
                        st.dataframe(r.drop(columns=["geometry", "_rid"], errors="ignore"), width="stretch")
        with st.expander("Journal de traitement"):
            st.code("\n".join(SS.log) or "—")


# ================================================================== 5. EXPORT
with tabs[4]:
    if SS.clean is None:
        st.info("Lancez d'abord le pré-traitement (onglet 4) : l'export porte sur le jeu pré-traité, avec statut QA.")
    else:
        fmts = st.multiselect("Formats", ["GeoPackage", "Shapefile", "KML"], ["GeoPackage", "Shapefile", "KML"])
        st.caption("Shapefile : noms de champs limités à 10 caractères → table de correspondance fournie. "
                   "KML : tous les attributs en ExtendedData, couleur selon le statut QA.")
        if st.button("📦 Générer les exports", type="primary", disabled=not fmts):
            with st.spinner("Écriture des fichiers…"):
                summ = qa.summarize(SS.clean, SS.issues_clean)
                SS.bundle = exporters.build_bundle(SS.clean, SS.rejected, SS.issues_clean, summ,
                                                   qa.completeness(SS.clean), SS.log, fmts)
                SS.exports = {
                    "GeoPackage": exporters.to_gpkg(SS.clean, SS.issues_clean, summ, SS.rejected) if "GeoPackage" in fmts else None,
                    "Shapefile": exporters.to_shp_zip(SS.clean) if "Shapefile" in fmts else None,
                    "KML": exporters.to_kml_files(SS.clean)["PCDN_toutes_couches.kml"] if "KML" in fmts else None,
                    "QA": exporters.qa_report_xlsx(SS.issues_clean, summ, qa.completeness(SS.clean), SS.log),
                }
        if SS.bundle:
            st.success("Exports prêts.")
            e = SS.exports
            st.download_button("⬇ Archive complète (ZIP : GPKG + SHP + KML + rapports QA)", SS.bundle, "PCDN_export.zip",
                               "application/zip", type="primary")
            c1, c2, c3, c4 = st.columns(4)
            if e["GeoPackage"]:
                c1.download_button("GeoPackage (.gpkg)", e["GeoPackage"], "PCDN.gpkg", "application/geopackage+sqlite3")
            if e["Shapefile"]:
                c2.download_button("Shapefiles (ZIP)", e["Shapefile"], "PCDN_shp.zip", "application/zip")
            if e["KML"]:
                c3.download_button("KML (toutes couches)", e["KML"], "PCDN.kml", "application/vnd.google-earth.kml+xml")
            c4.download_button("Rapport QA (.xlsx)", e["QA"], "rapport_QA.xlsx",
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
