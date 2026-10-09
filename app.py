"""PCDN – monitoring des données géospatiales (Streamlit).
Même logique que le pipeline R : FTP -> lecture .shp/.kml/.zip -> reconnaissance des couches -> contrôle qualité
-> consolidation -> exports (GPKG, Excel, QA, corrections par agent, suivi quotidien).
Lancer :  streamlit run app.py
"""
from __future__ import annotations

import io
from datetime import date

import pandas as pd
import pydeck as pdk
import streamlit as st

from pcdn import ftp
from pcdn.exporters import _write_sheets, export_all, summarise_quality, zip_latest
from pcdn.pipeline import run_pipeline
from pcdn.settings import get_settings, load_schema
from pcdn.util import Logger

st.set_page_config(page_title="PCDN – Monitoring", page_icon="🚉", layout="wide")
S, SCHEMA = get_settings(), None


@st.cache_data
def _schema():
    return load_schema(S["config_dir"])


SCHEMA = _schema()
SS = st.session_state
for k, v in {"res": None, "log": [], "exported": False}.items():
    SS.setdefault(k, v)


def local_files() -> pd.DataFrame:
    f = [p for p in S["dir_ftp"].rglob("*") if p.is_file() and p.suffix.lower() in (".shp", ".kml", ".zip")] if S["dir_ftp"].exists() else []
    return pd.DataFrame({"fichier": [str(p.relative_to(S["dir_ftp"])) for p in f],
                         "taille_ko": [round(p.stat().st_size / 1024, 1) for p in f],
                         "modifié": [pd.Timestamp(p.stat().st_mtime, unit="s").strftime("%Y-%m-%d %H:%M") for p in f]}).sort_values("modifié", ascending=False)


def run(with_ftp: bool):
    log = Logger(S["dir_logs"] / f"run_{date.today():%Y%m%d}.log")
    bar = st.progress(0.0, text="Démarrage…")
    log("===== Démarrage du monitoring PCDN =====")
    if with_ftp:
        try:
            bar.progress(0.05, text="Synchronisation FTP…")
            ftp.sync(S, log, progress=lambda p: bar.progress(0.05 + 0.2 * p, text="Téléchargement FTP…"))
        except Exception as e:
            log(f"FTP indisponible : {e} -> traitement des fichiers déjà présents", level="ERROR")
            st.warning(f"FTP indisponible : {e}. Traitement des fichiers déjà présents.")
    res = run_pipeline(S, SCHEMA, log, progress=lambda p: bar.progress(0.25 + 0.75 * p, text="Lecture et contrôle…"))
    bar.empty()
    SS.res, SS.log, SS.exported = res, log.lines, False
    if not res["data"]:
        st.error("Aucune donnée exploitable trouvée. Vérifiez l'onglet « Collecte ».")


# ------------------------------------------------------------------------------------------------
st.title("🚉 PCDN – Monitoring des données de terrain")
tabs = st.tabs(["1 · Collecte", "2 · Traitement", "3 · Tableau de bord", "4 · Anomalies", "5 · Données & carte", "6 · Export"])

# ---- 1. Collecte ---------------------------------------------------------------------------------
with tabs[0]:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Serveur FTP")
        st.caption("Identifiants lus dans `PCDN_FTP_HOST`, `PCDN_FTP_USER`, `PCDN_FTP_PWD` (variables d'environnement ou `.streamlit/secrets.toml`).")
        configured = all(ftp.env(k) for k in ("PCDN_FTP_HOST", "PCDN_FTP_USER", "PCDN_FTP_PWD"))
        st.write("Configuration FTP :", "✅ renseignée" if configured else "❌ incomplète")
        if st.button("⬇️ Télécharger les nouveaux fichiers", disabled=not configured):
            log = Logger(S["dir_logs"] / f"run_{date.today():%Y%m%d}.log")
            bar = st.progress(0.0)
            try:
                n = ftp.sync(S, log, progress=bar.progress)
                st.success(f"{n} fichier(s) nouveau(x) ou modifié(s) téléchargé(s).")
            except Exception as e:
                st.error(f"FTP : {e}")
            bar.empty()
    with c2:
        st.subheader("Ou déposer des fichiers")
        up = st.file_uploader("Fichiers .kml, .zip (shapefile) ou .shp + .shx/.dbf/.prj/.cpg", accept_multiple_files=True,
                              type=["kml", "zip", "shp", "shx", "dbf", "prj", "cpg"])
        if up and st.button("📥 Enregistrer les fichiers déposés"):
            d = S["dir_ftp"] / "depots"
            d.mkdir(parents=True, exist_ok=True)
            for f in up:
                (d / f.name).write_bytes(f.getbuffer())
            st.success(f"{len(up)} fichier(s) enregistré(s).")
    st.subheader("Fichiers disponibles localement")
    lf = local_files()
    st.caption(f"{len(lf)} fichier(s) .shp / .kml / .zip")
    st.dataframe(lf, width="stretch", hide_index=True)

# ---- 2. Traitement ---------------------------------------------------------------------------------
with tabs[1]:
    st.write("Lecture → reconnaissance de la couche → harmonisation → contrôles → consolidation.")
    cc1, cc2 = st.columns(2)
    if cc1.button("▶️ Traiter les fichiers présents", type="primary"):
        run(with_ftp=False)
    if cc2.button("🔄 FTP + traitement"):
        run(with_ftp=True)
    if SS.res:
        st.subheader("Journal des fichiers")
        fl = SS.res["file_log"]
        st.dataframe(fl, width="stretch", hide_index=True)
        if (fl["statut"] == "NON RECONNU").any():
            st.warning("Certains fichiers n'ont pas été rattachés à une couche (nom de fichier ou colonnes inattendus).")
        with st.expander("Journal d'exécution"):
            st.code("\n".join(SS.log))

# ---- 3. Tableau de bord --------------------------------------------------------------------------------
with tabs[2]:
    if not SS.res or not SS.res["data"]:
        st.info("Lancez le traitement (onglet 2) pour voir les résultats.")
    else:
        res = SS.res
        q, iss = summarise_quality(res), res["issues_all"]
        tot = int(q["entites"].sum())
        nerr, nwar = int((iss.severity == "error").sum()), int((iss.severity == "warning").sum())
        n_bad = int(q["entites_avec_erreur"].sum())
        k = st.columns(5)
        k[0].metric("Entités consolidées", tot)
        k[1].metric("Erreurs", nerr)
        k[2].metric("Avertissements", nwar)
        k[3].metric("Entités conformes", f"{100 * (1 - n_bad / max(tot, 1)):.1f} %")
        k[4].metric("Couches renseignées", f"{len(res['data'])}/{len(SCHEMA['layers'])}")
        st.subheader("Synthèse par couche")
        st.dataframe(q, width="stretch", hide_index=True)
        missing = sorted(set(SCHEMA["layers"]["layer"]) - set(res["data"]))
        if missing:
            st.warning("Couches sans aucune donnée : " + ", ".join(missing))
        a, b = st.columns(2)
        with a:
            st.subheader("Anomalies par type")
            if len(iss):
                st.bar_chart(iss.groupby("type").size().sort_values(ascending=False), horizontal=True)
        with b:
            st.subheader("Anomalies par agent")
            if len(iss):
                st.bar_chart(iss.groupby("agent").size().sort_values(ascending=False))
        hp = S["dir_output"] / "suivi_quotidien.csv"
        if hp.exists():
            h = pd.read_csv(hp)
            st.subheader("Suivi quotidien")
            st.line_chart(h.pivot_table(index="date_execution", columns="couche", values="entites", aggfunc="sum"))
            with st.expander("Historique (taux de conformité, erreurs)"):
                st.dataframe(h, width="stretch", hide_index=True)

# ---- 4. Anomalies ---------------------------------------------------------------------------------------
with tabs[3]:
    if not SS.res or not len(SS.res["issues_all"]):
        st.info("Aucune anomalie à afficher.")
    else:
        iss = SS.res["issues_all"]
        f1, f2, f3, f4 = st.columns(4)
        lay = f1.multiselect("Couche", sorted(iss["layer"].unique()))
        sev = f2.multiselect("Gravité", ["error", "warning"], default=["error", "warning"])
        typ = f3.multiselect("Type", sorted(iss["type"].unique()))
        ag = f4.multiselect("Agent", sorted(iss["agent"].dropna().unique()))
        v = iss[iss["severity"].isin(sev)]
        for col, sel in (("layer", lay), ("type", typ), ("agent", ag)):
            if sel:
                v = v[v[col].isin(sel)]
        st.caption(f"{len(v)} anomalie(s)")
        st.dataframe(v, width="stretch", hide_index=True, height=480)
        d1, d2 = st.columns(2)
        d1.download_button("⬇️ Anomalies filtrées (CSV)", v.to_csv(index=False, encoding="utf-8-sig"), "anomalies.csv", "text/csv")
        who = d2.selectbox("Classeur de corrections pour l'agent", sorted(iss["agent"].dropna().unique()))
        if who:
            buf = io.BytesIO()
            tmp = S["dir_work"] / "_agent.xlsx"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            _write_sheets(tmp, {"Corrections": iss[iss["agent"] == who]})
            d2.download_button(f"⬇️ Corrections – {who}", tmp.read_bytes(), f"corrections_{who}.xlsx")

# ---- 5. Données & carte ------------------------------------------------------------------------------------
with tabs[4]:
    if not SS.res or not SS.res["data"]:
        st.info("Lancez le traitement (onglet 2).")
    else:
        name = st.selectbox("Couche", list(SS.res["data"]))
        g = SS.res["data"][name]
        shown = g[[c for c in g.columns if not c.startswith("x_") and c != "_multigeom"]]
        st.dataframe(pd.DataFrame(shown.drop(columns="geometry")), width="stretch", hide_index=True, height=300)
        g = g[~(g.geometry.isna() | g.geometry.is_empty)]
        if len(g):
            col = [[220, 60, 60, 200] if e > 0 else ([245, 155, 30, 200] if w > 0 else [60, 180, 90, 200])
                   for e, w in zip(g["_n_err"], g["_n_warn"])]
            lid = SCHEMA["layers"].set_index("layer").loc[name, "id_var"]
            base = pd.DataFrame({"id": g[lid].fillna("(sans id)").to_numpy(), "color": col, "agent": g["_agent"].to_numpy()})
            if g.geometry.iloc[0].geom_type.endswith("Point"):
                base["lon"], base["lat"] = g.geometry.x.to_numpy(), g.geometry.y.to_numpy()
                layer = pdk.Layer("ScatterplotLayer", base, get_position="[lon, lat]", get_fill_color="color", get_radius=6,
                                  radius_units="pixels", pickable=True)
            else:
                base["path"] = [list(map(list, (gm.geoms[0] if hasattr(gm, "geoms") else gm).coords)) for gm in g.geometry]
                layer = pdk.Layer("PathLayer", base, get_path="path", get_color="color", get_width=4, width_units="pixels", pickable=True)
            minx, miny, maxx, maxy = g.total_bounds
            st.pydeck_chart(pdk.Deck(layers=[layer], tooltip={"text": "{id}\n{agent}"},
                                     initial_view_state=pdk.ViewState(longitude=(minx + maxx) / 2, latitude=(miny + maxy) / 2,
                                                                      zoom=7 if (maxx - minx) > 0.5 else 11)))
            st.caption("🟢 conforme · 🟠 avertissement · 🔴 erreur")

# ---- 6. Export -----------------------------------------------------------------------------------------------
with tabs[5]:
    if not SS.res or not SS.res["data"]:
        st.info("Lancez le traitement (onglet 2).")
    else:
        extra = st.multiselect("Formats supplémentaires (en plus du GeoPackage et des Excel)", ["shp", "kml"], default=["shp", "kml"])
        if st.button("💾 Générer les exports", type="primary"):
            log = Logger(S["dir_logs"] / f"run_{date.today():%Y%m%d}.log")
            with st.spinner("Écriture des fichiers…"):
                export_all(SS.res, SCHEMA, S, log, formats=["gpkg", "xlsx", *extra])
            SS.exported = True
            st.success("Exports générés (dossier `local/output/latest`, copie datée dans `archive/`).")
        if SS.exported:
            st.download_button("⬇️ Télécharger tout (ZIP)", zip_latest(S), f"PCDN_export_{date.today():%Y%m%d}.zip", "application/zip")
        st.markdown("**Contenu** : `pcdn_donnees.gpkg` · `pcdn_donnees.xlsx` · `controle_qualite.xlsx` · `corrections_par_agent/*.xlsx` · "
                    "`shp/` · `kml/` · `suivi_quotidien.csv`")
