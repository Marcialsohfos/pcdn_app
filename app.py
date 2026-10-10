"""PCDN – collecte, fusion BRUTE par couche, export (GPKG / SHP / KML) et rapport de traitement (Streamlit).
Aucune donnée n'est supprimée ni modifiée : les contrôles signalent, les équipes de traitement décident.
Lancer :  streamlit run app.py
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pydeck as pdk
import streamlit as st

from pcdn import ftp
from pcdn.exporters import ACTIONS, _write_sheets, export_all, plan_action, summarise_quality, zip_latest
from pcdn.pipeline import run_pipeline
from pcdn.settings import get_settings, load_schema
from pcdn.util import Logger

st.set_page_config(page_title="PCDN – Monitoring", page_icon="🚉", layout="wide")
S = get_settings()


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
    res = run_pipeline(S, SCHEMA, log, progress=lambda p: bar.progress(0.25 + 0.75 * p, text="Lecture, fusion et contrôle…"))
    bar.empty()
    SS.res, SS.log, SS.exported = res, log.lines, False
    if not res["data"]:
        st.error("Aucune donnée exploitable trouvée. Vérifiez l'onglet « Collecte ».")


st.title("🚉 PCDN – Monitoring des données de terrain")
st.caption("Fusion brute par couche : aucune ligne n'est supprimée ni modifiée. Les anomalies sont signalées dans le rapport de traitement.")
tabs = st.tabs(["1 · Collecte", "2 · Fusion & contrôle", "3 · Tableau de bord", "4 · Rapport de traitement", "5 · Données & carte", "6 · Export"])

# ---- 1. Collecte -------------------------------------------------------------------------------------
with tabs[0]:
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Serveur FTP")
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

# ---- 2. Fusion & contrôle -----------------------------------------------------------------------------
with tabs[1]:
    st.write("Lecture de tous les fichiers → reconnaissance de la couche → **fusion brute** par couche → contrôle (marquage seulement).")
    cc1, cc2 = st.columns(2)
    if cc1.button("▶️ Fusionner les fichiers présents", type="primary"):
        run(with_ftp=False)
    if cc2.button("🔄 FTP + fusion"):
        run(with_ftp=True)
    if SS.res:
        fl = SS.res["file_log"]
        st.subheader("Journal des fichiers")
        st.dataframe(fl, width="stretch", hide_index=True)
        if (fl["statut"] == "NON RECONNU").any():
            st.warning("Certains fichiers n'ont pas été rattachés à une couche : ils ne sont PAS dans les exports. Vérifiez leur nom.")
        with st.expander("Journal d'exécution"):
            st.code("\n".join(SS.log))

# ---- 3. Tableau de bord ----------------------------------------------------------------------------------
with tabs[2]:
    if not SS.res or not SS.res["data"]:
        st.info("Lancez la fusion (onglet 2).")
    else:
        res = SS.res
        q, iss = summarise_quality(res), res["issues_all"]
        tot = int(q["entites"].sum())
        k = st.columns(5)
        k[0].metric("Lignes brutes consolidées", tot)
        k[1].metric("À corriger", int(q["a_corriger"].sum()))
        k[2].metric("À vérifier", int(q["a_verifier"].sum()))
        k[3].metric("Conformes", f"{100 * q['conformes'].sum() / max(tot, 1):.1f} %")
        k[4].metric("Lignes en doublon", int(q["entites_en_doublon"].sum()))
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
            st.subheader("Lignes à corriger par agent")
            rows = pd.concat(res["rows"].values())
            st.bar_chart(rows[rows["statut"] == "À corriger"].groupby("agent").size().sort_values(ascending=False))
        hp = S["dir_output"] / "suivi_quotidien.csv"
        if hp.exists():
            h = pd.read_csv(hp)
            st.subheader("Suivi quotidien")
            st.line_chart(h.pivot_table(index="date_execution", columns="couche", values="entites", aggfunc="sum"))

# ---- 4. Rapport de traitement ----------------------------------------------------------------------------
with tabs[3]:
    if not SS.res or not SS.res["data"]:
        st.info("Lancez la fusion (onglet 2).")
    else:
        res = SS.res
        st.subheader("Plan d'action (par où commencer)")
        st.dataframe(plan_action(res["issues_all"]), width="stretch", hide_index=True, height=300)
        rows = pd.concat(res["rows"].values(), ignore_index=True)
        f1, f2, f3 = st.columns(3)
        lay = f1.multiselect("Couche", sorted(rows["couche"].unique()))
        sta = f2.multiselect("Statut", ["À corriger", "À vérifier", "Conforme"], default=["À corriger", "À vérifier"])
        ag = f3.multiselect("Agent", sorted(rows["agent"].dropna().unique()))
        v = rows[rows["statut"].isin(sta)]
        for col, sel in (("couche", lay), ("agent", ag)):
            if sel:
                v = v[v[col].isin(sel)]
        st.subheader(f"Lignes à traiter ({len(v)})")
        st.dataframe(v[["couche", "statut", "fichier_source", "ligne_source", "id", "nom", "agent", "date", "problemes", "recommandation_doublon"]],
                     width="stretch", hide_index=True, height=420)
        iss = res["issues_all"]
        who = st.selectbox("Classeur de corrections pour l'agent", sorted(iss["agent"].dropna().unique()) if len(iss) else [])
        if who:
            tmp = S["dir_work"] / "_agent.xlsx"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            _write_sheets(tmp, {"Corrections": iss[iss["agent"] == who]})
            st.download_button(f"⬇️ Corrections – {who}", tmp.read_bytes(), f"corrections_{who}.xlsx")
        st.caption("Le rapport complet (Lisez-moi, Plan d'action, A traiter, Doublons, Champs vides, Couverture par zone…) est généré dans l'onglet Export.")

# ---- 5. Données & carte ------------------------------------------------------------------------------------
with tabs[4]:
    if not SS.res or not SS.res["data"]:
        st.info("Lancez la fusion (onglet 2).")
    else:
        name = st.selectbox("Couche", list(SS.res["data"]))
        g, r = SS.res["data"][name], SS.res["rows"][name]
        view = pd.DataFrame(g.drop(columns="geometry")).drop(columns=["_fmt", "_file_date", "_multigeom"], errors="ignore")
        view.insert(0, "statut_qa", r["statut"].to_numpy())
        st.dataframe(view, width="stretch", hide_index=True, height=300)
        ok = ~(g.geometry.isna() | g.geometry.is_empty).to_numpy()
        gg, rr = g[ok], r[ok]
        if len(gg):
            col = [[220, 60, 60, 200] if e > 0 else ([245, 155, 30, 200] if w > 0 else [60, 180, 90, 200]) for e, w in zip(rr["n_err"], rr["n_warn"])]
            base = pd.DataFrame({"id": rr["id"].fillna("(sans id)").to_numpy(), "color": col, "agent": rr["agent"].to_numpy()})
            if gg.geometry.iloc[0].geom_type.endswith("Point"):
                base["lon"], base["lat"] = gg.geometry.x.to_numpy(), gg.geometry.y.to_numpy()
                layer = pdk.Layer("ScatterplotLayer", base, get_position="[lon, lat]", get_fill_color="color", get_radius=6, radius_units="pixels", pickable=True)
            else:
                base["path"] = [list(map(list, (gm.geoms[0] if hasattr(gm, "geoms") else gm).coords)) for gm in gg.geometry]
                layer = pdk.Layer("PathLayer", base, get_path="path", get_color="color", get_width=4, width_units="pixels", pickable=True)
            minx, miny, maxx, maxy = gg.total_bounds
            st.pydeck_chart(pdk.Deck(layers=[layer], tooltip={"text": "{id}\n{agent}"},
                                     initial_view_state=pdk.ViewState(longitude=(minx + maxx) / 2, latitude=(miny + maxy) / 2,
                                                                      zoom=7 if (maxx - minx) > 0.5 else 11)))
            st.caption("🟢 conforme · 🟠 à vérifier · 🔴 à corriger")

# ---- 6. Export ---------------------------------------------------------------------------------------------
with tabs[5]:
    if not SS.res or not SS.res["data"]:
        st.info("Lancez la fusion (onglet 2).")
    else:
        fm = st.multiselect("Formats des données brutes", ["gpkg", "shp", "kml"], default=["gpkg", "shp", "kml"])
        if st.button("💾 Générer les exports et le rapport", type="primary"):
            log = Logger(S["dir_logs"] / f"run_{date.today():%Y%m%d}.log")
            with st.spinner("Écriture des fichiers…"):
                export_all(SS.res, SCHEMA, S, log, formats=fm)
            SS.exported = True
            st.success("Exports générés (dossier `local/output/latest`, copie datée dans `archive/`).")
            if any("ERROR" in l for l in log.lines):
                st.warning("Certaines couches n'ont pas pu être écrites dans un format : voir le journal.")
        if SS.exported:
            st.download_button("⬇️ Télécharger tout (ZIP)", zip_latest(S), f"PCDN_export_{date.today():%Y%m%d}.zip", "application/zip")
        st.markdown("**Contenu du ZIP** : `donnees_brutes/` (`pcdn_brut.gpkg`, `shp/`, `kml/`) · `rapport/rapport_traitement.xlsx` · "
                    "`rapport/corrections_par_agent/` · `suivi_quotidien.csv`")
        st.caption("Données brutes = toutes les lignes reçues, valeurs inchangées. Deux colonnes de traçabilité sont ajoutées : "
                   "`fichier_source` et `ligne_source`.")
