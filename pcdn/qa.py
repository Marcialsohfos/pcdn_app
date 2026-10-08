"""Assurance qualité : structure, géométrie, ID, domaines, types, plages, logique métier, inter-couches."""
from __future__ import annotations

import difflib
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.strtree import STRtree

from .config import BBOX, ID_RE, LAYERS, Layer
from .utils import clean_text, is_null, norm, parse_bool, parse_num, split_multi

COLS = ["layer", "_rid", "zone", "id", "field", "rule", "severity", "message", "suggestion"]
RANGES = {  # champ: (min, max, sévérité)
    "densite_couvert": (0, 100, "ERROR"), "temps_acces_min": (0, 600, "WARNING"),
    "largeur_m": (0.5, 40, "WARNING"), "vitesse_moy_kmh": (3, 140, "WARNING"),
    "duree_coupure_jours": (0, 365, "ERROR"), "capacite_stockage_t": (0, 1e7, "ERROR"),
}


# ---------------------------------------------------------------- vue typée
def typed_view(df: pd.DataFrame, spec: Layer):
    """Copie typée (bool/num/texte nettoyé) + masques de valeurs invalides."""
    out, inv, nonint = df.copy(), {}, {}
    for f in spec.fields:
        if f.typ == "BOOLEAN":
            out[f.name], inv[f.name] = parse_bool(df[f.name])
        elif f.typ in ("DOUBLE", "INTEGER"):
            out[f.name], inv[f.name] = parse_num(df[f.name])
            if f.typ == "INTEGER":
                nonint[f.name] = out[f.name].notna() & (out[f.name] % 1 != 0)
        else:
            out[f.name] = df[f.name].map(clean_text)
    return out, inv, nonint


def domain_lookup(values):
    return {norm(v): v for v in values}


def match_domain(tok: str, values: list[str], nmap: dict, cutoff=0.0):
    """-> (statut, valeur_canonique) ; statut: ok | case | free | fuzzy | none"""
    if tok in values:
        return "ok", tok
    n = norm(tok)
    if n in nmap:
        return "case", nmap[n]
    if n.startswith("autre") and any(norm(v).startswith("autre") for v in values):
        return "free", tok
    if cutoff:
        m = difflib.get_close_matches(n, list(nmap), n=1, cutoff=cutoff)
        if m:
            return "fuzzy", nmap[m[0]]
    return "none", tok


# ---------------------------------------------------------------- logique métier
def _eq(s, *vals):
    ns = {norm(v) for v in vals}
    return s.map(lambda x: norm(x) in ns if x is not None else False)


def _ct(s, val):
    n = norm(val)
    return s.map(lambda x: n in norm(x) if x is not None else False)


def _tb(s):
    return (s == True).fillna(False).astype(bool)  # noqa: E712


def _fb(s):
    return (s == False).fillna(False).astype(bool)  # noqa: E712


def _nn(s):
    return s.notna()


NA_STOCK = ("Non applicable",)
LOGIC = {
    "Gares": [
        ("L_MAG1", "Aucun magasin déclaré mais un type de magasin est renseigné",
         lambda d: _fb(d.presence_magasin_gare) & _nn(d.type_magasin)),
        ("L_MAG2", "Aucun magasin déclaré mais une capacité de stockage est renseignée",
         lambda d: _fb(d.presence_magasin_gare) & _nn(d.capacite_stockage_gare) & ~_eq(d.capacite_stockage_gare, *NA_STOCK)),
        ("L_MAG3", "Magasin présent sans type de magasin", lambda d: _tb(d.presence_magasin_gare) & d.type_magasin.isna()),
        ("L_TEL", "« Aucune couverture » mais un opérateur est renseigné",
         lambda d: _eq(d.reseau_telecom, "Aucune couverture") & _nn(d.operateur_telecom)),
    ],
    "Infrastructures_Ferroviaires": [
        ("L_PN", "Un passage à niveau ne devrait pas avoir de quai d'embarquement",
         lambda d: _eq(d.type_infra, "Passage à niveau") & _tb(d.quai_embarq)),
    ],
    "Points_Vente_Marches": [
        ("L_STK1", "Pas de magasin de stockage mais capacité renseignée",
         lambda d: _fb(d.presence_magasin_stockage) & _nn(d.capacite_stockage) & ~_eq(d.capacite_stockage, *NA_STOCK)),
        ("L_AIRE1", "Pas d'aire de chargement mais un type d'aire est renseigné",
         lambda d: _fb(d.presence_aire_chargement) & _nn(d.type_aire_chargement)),
        ("L_AIRE2", "Aire de chargement présente sans type", lambda d: _tb(d.presence_aire_chargement) & d.type_aire_chargement.isna()),
    ],
    "Reseau_Routier_Pistes": [
        ("L_CRIT1", "Point critique = oui mais aucun blocage / obstacle indiqué",
         lambda d: _tb(d.pt_critique) & (d.type_blocage.isna() | _eq(d.type_blocage, "Aucun"))),
        ("L_CRIT2", "Blocage indiqué alors que point critique = non",
         lambda d: _fb(d.pt_critique) & _nn(d.type_blocage) & ~_eq(d.type_blocage, "Aucun")),
        ("L_PRAT", "Accessible toute l'année mais coupures > 0 jour/an",
         lambda d: _eq(d.praticabilite_pluie, "Accessible toute année (Toutes voitures)") & (d.duree_coupure_jours > 0).fillna(False)),
        ("L_SURF", "Route nationale/régionale avec revêtement en terre/sable",
         lambda d: _eq(d.categorie_voie, "Route Nationale (RN)", "Route Régionale") & _eq(d.type_surface, "Terre", "Sable")),
    ],
    "Activites_autour_gare": [
        ("L_CULT", "Type de culture renseigné hors activité agricole",
         lambda d: _nn(d.type_culture) & ~_eq(d.type_activite, "Agriculture – champs riverains")),
        ("L_ELEV", "Type d'élevage renseigné hors activité pastorale",
         lambda d: _nn(d.type_elevage) & ~_eq(d.type_activite, "Pâturage – élevage extensif")),
        ("L_BETAIL", "Traversée de bétail déclarée mais pas de point de traversée informelle",
         lambda d: _eq(d.traversee_betail, "Oui, fréquente", "Oui, occasionnelle") & _fb(d.point_traversee_informelle)),
        ("L_EMPRISE", "Localisation « dans l'emprise » incohérente avec la distance à la voie",
         lambda d: _eq(d.localisation_gare, "Dans l'emprise ferroviaire") & _eq(d.distance_voie, "50–200 m", "> 200 m")),
    ],
    "Ouvrage_franchissement": [
        ("L_GARD1", "Gardien-barrières présent dans les dispositifs mais « Présence de gardien » = Non",
         lambda d: _eq(d.presence_gardien, "Non") & _ct(d.dispositif_securite, "Gardien-barrières présent")),
        ("L_GARD2", "Gardien présent mais dispositif = « Aucun dispositif »",
         lambda d: d.presence_gardien.map(lambda x: norm(x).startswith("oui") if x else False) & _ct(d.dispositif_securite, "Aucun dispositif")),
        ("L_PNG", "PN gardé sans gardien-barrières", lambda d: _eq(d.type_ouvrage, "Passage à niveau (PN) gardé") & _eq(d.presence_gardien, "Non")),
        ("L_SAUV", "Traversée sauvage : le type d'ouvrage devrait être « Passage informel »",
         lambda d: _eq(d.statut_ouvrage, "Traversée sauvage (interdite)") & _nn(d.type_ouvrage) & ~_eq(d.type_ouvrage, "Passage informel (traversée sauvage)", "Autre (préciser)")),
    ],
    "Ground_Truthing_LandCover": [
        ("L_FOR", "Forêt dense avec densité de couvert < 40 %", lambda d: _eq(d.classe_reelle, "Forêt dense / Couvert arborescent") & (d.densite_couvert < 40).fillna(False)),
        ("L_SOL", "Sol nu avec densité de couvert > 30 %", lambda d: _eq(d.classe_reelle, "Sol nu / Zone d'extraction") & (d.densite_couvert > 30).fillna(False)),
    ],
    "Services_Sociaux_Bases": [
        ("L_EQ", "Type d'équipement incohérent avec le domaine d'activité",
         lambda d: pd.Series([bool(e and dom and " - " in norm(e) and norm(e).split(" - ")[0] != norm(dom)
                                   and not norm(dom).startswith("autre") and not norm(e).startswith("autre"))
                              for e, dom in zip(d.type_equipement, d.domaine_activite)], index=d.index)),
    ],
}
EXCLUSIVE = ("aucun",)  # valeurs exclusives dans les listes à choix multiple


# ---------------------------------------------------------------- QA d'une couche
def run_layer_qa(gdf: gpd.GeoDataFrame, layer: str, domains: dict, dup_tol_m: float = 3.0,
                 struct: pd.DataFrame | None = None) -> pd.DataFrame:
    spec = LAYERS[layer]
    dom = domains.get(layer, {})
    rows: list = []
    idf = spec.id_field
    # dtype=object obligatoire : avec pandas 3, Series.map(...) reconvertit None en NaN (float)
    ids = pd.Series([None if is_null(v) else str(v).strip() for v in gdf[idf]], index=gdf.index, dtype="object")
    zsrc = gdf["zone"] if "zone" in gdf else [None] * len(gdf)
    zones = pd.Series([None if is_null(v) else str(v).strip() for v in zsrc], index=gdf.index, dtype="object")

    def add(i, field, rule, sev, msg, sugg=""):
        rows.append((layer, gdf["_rid"].iat[i] if i is not None else None,
                     zones.iat[i] if i is not None else None, ids.iat[i] if i is not None else None,
                     field, rule, sev, msg, sugg))

    # --- structure (issues remontées par l'ingestion)
    if struct is not None and len(struct):
        for r in struct[struct.layer == layer].itertuples():
            rows.append((layer, None, r.zone, None, r.field, r.rule, r.severity, r.message, ""))

    # --- géométrie
    g = gdf.geometry
    null = (g.isna() | g.is_empty).to_numpy()
    valid = g.is_valid.to_numpy()
    gtype = g.geom_type.to_numpy()
    bounds = g.bounds.to_numpy()
    for i in range(len(gdf)):
        if null[i]:
            add(i, "geometry", "GEOM_NULL", "ERROR", "Géométrie absente ou vide")
            continue
        if gtype[i] != spec.geom:
            add(i, "geometry", "GEOM_TYPE", "ERROR", f"Type {gtype[i]} au lieu de {spec.geom}")
        if not valid[i]:
            add(i, "geometry", "GEOM_INVALID", "ERROR", "Géométrie invalide (auto-intersection…)", "make_valid")
        minx, miny, maxx, maxy = bounds[i]
        if spec.geom == "Point" and minx == 0 and miny == 0:
            add(i, "geometry", "GEOM_ZERO", "ERROR", "Coordonnées (0,0) : GPS non fixé")
        elif maxx < BBOX[0] or minx > BBOX[2] or maxy < BBOX[1] or miny > BBOX[3]:
            add(i, "geometry", "GEOM_BBOX", "ERROR", f"Hors du Cameroun ({minx:.4f}, {miny:.4f})",
                "Inversion lat/lon ou erreur GPS ?")
    ok = ~null
    if ok.sum() > 1:
        try:
            gg = gdf.loc[ok, ["geometry"]].to_crs(gdf.loc[ok].estimate_utm_crs())
            idx = np.flatnonzero(ok)
            if spec.geom == "Point":
                tree = STRtree(gg.geometry.values)
                a, b = tree.query(gg.geometry.values, predicate="dwithin", distance=dup_tol_m)
                seen = set()
                for x, y in zip(a, b):
                    if x < y and (x, y) not in seen:
                        seen.add((x, y))
                        add(idx[x], "geometry", "GEOM_DUP", "WARNING",
                            f"Point à < {dup_tol_m:g} m de {ids.iat[idx[y]]} (doublon possible)")
                        add(idx[y], "geometry", "GEOM_DUP", "WARNING",
                            f"Point à < {dup_tol_m:g} m de {ids.iat[idx[x]]} (doublon possible)")
            else:
                for x, ln in zip(idx, gg.geometry.length):
                    if ln < 5:
                        add(x, "geometry", "LINE_SHORT", "WARNING", f"Tronçon très court ({ln:.1f} m)")
        except Exception as e:  # noqa
            add(None, "geometry", "QA_INTERNAL", "INFO", f"Contrôle de proximité non réalisé: {e}")

    # --- identifiants
    dup = ids.duplicated(keep=False) & ids.notna()
    for i in range(len(gdf)):
        v = ids.iat[i]
        if v is None or pd.isna(v) or not str(v).strip():
            add(i, idf, "ID_MISSING", "ERROR", "Identifiant manquant")
            continue
        v = str(v).strip()
        m = ID_RE.match(v)
        zexp = zones.iat[i]
        if not m:
            add(i, idf, "ID_FORMAT", "ERROR", f"Format attendu ZONE<n>{spec.prefix}<n> (ex. ZONE1{spec.prefix}1), reçu « {v} »",
                f"{zexp or 'ZONEn'}{spec.prefix}n")
            continue
        if m.group(2) != spec.prefix:
            add(i, idf, "ID_PREFIX", "ERROR", f"Préfixe {m.group(2)} ≠ {spec.prefix} attendu pour cette couche")
        if zexp and f"ZONE{m.group(1)}" != zexp:
            add(i, idf, "ID_ZONE", "ERROR", f"ID de ZONE{m.group(1)} mais fichier de {zexp}")
        if dup.iat[i]:
            add(i, idf, "ID_DUP", "ERROR", "Identifiant en double")
    seqs: dict = {}
    for v in ids.dropna():
        m = ID_RE.match(v)
        if m and m.group(2) == spec.prefix:
            seqs.setdefault(f"ZONE{m.group(1)}", set()).add(int(m.group(3)))
    for z, s in seqs.items():
        miss = sorted(set(range(1, max(s) + 1)) - s)
        if miss:
            rows.append((layer, None, z, None, idf, "ID_GAP", "INFO",
                         f"Numéros manquants dans la séquence: {miss[:15]}{'…' if len(miss) > 15 else ''}", ""))

    # --- types, domaines, obligatoires
    d, inv, nonint = typed_view(gdf, spec)
    for f in spec.fields:
        col = d[f.name]
        if f.req and f.name != idf:
            for i in np.flatnonzero(col.isna().to_numpy()):
                add(i, f.name, "REQ_MISSING", "ERROR" if f.req == "E" else "WARNING", f"Champ « {f.name} » non renseigné")
        if f.typ in ("BOOLEAN", "DOUBLE", "INTEGER"):
            for i in np.flatnonzero(inv[f.name].to_numpy()):
                add(i, f.name, "TYPE_INVALID", "ERROR", f"Valeur « {gdf[f.name].iat[i]} » non {'booléenne' if f.typ == 'BOOLEAN' else 'numérique'}")
            for i in np.flatnonzero(nonint.get(f.name, pd.Series(False, index=d.index)).to_numpy()):
                add(i, f.name, "TYPE_DECIMAL", "WARNING", f"Entier attendu, reçu {d[f.name].iat[i]}", "arrondi")
            if f.name in RANGES:
                lo, hi, sev = RANGES[f.name]
                bad = ((col < lo) | (col > hi)).fillna(False).to_numpy()
                for i in np.flatnonzero(bad):
                    add(i, f.name, "RANGE", sev, f"{f.name}={col.iat[i]} hors plage [{lo}, {hi}]")
        elif f.dom and dom.get(f.dom):
            values = dom[f.dom]
            nmap = domain_lookup(values)
            for i in np.flatnonzero(col.notna().to_numpy()):
                toks = split_multi(col.iat[i], set(nmap)) if f.typ == "MULTISELECT" else [col.iat[i]]
                for t in toks:
                    st, canon = match_domain(t, values, nmap, cutoff=0.8)
                    if st == "case":
                        add(i, f.name, "DOM_CASE", "WARNING", f"« {t} » : casse/accents/tiret non conformes", canon)
                    elif st == "fuzzy":
                        add(i, f.name, "DOM_INVALID", "ERROR", f"« {t} » absent du dictionnaire", f"Proche de : {canon}")
                    elif st == "none":
                        add(i, f.name, "DOM_INVALID", "ERROR", f"« {t} » absent du dictionnaire")
                if f.typ == "MULTISELECT" and len(toks) > 1 and any(norm(t).startswith(EXCLUSIVE) for t in toks):
                    add(i, f.name, "MULTI_EXCL", "WARNING", "Valeur « Aucun » combinée à d'autres choix")
        if f.typ == "PICTURE" and f.req == "W":
            pass  # déjà couvert par REQ_MISSING

    # --- logique métier
    for code, msg, fn in LOGIC.get(layer, []):
        try:
            m = fn(d).fillna(False).to_numpy()
        except Exception as e:  # noqa
            add(None, "", code, "INFO", f"Règle {code} non évaluée: {e}")
            continue
        for i in np.flatnonzero(m):
            add(i, "", code, "WARNING", msg)

    return pd.DataFrame(rows, columns=COLS)


# ---------------------------------------------------------------- inter-couches
def run_cross_qa(layers: dict, domains: dict, max_gare_m: float = 300.0) -> pd.DataFrame:
    rows = []

    def utm(g):
        return g.to_crs(g.estimate_utm_crs())

    def clean(g):
        return g[g.geometry.notna() & ~g.geometry.is_empty]

    ga = layers.get("Gares")
    inf = layers.get("Infrastructures_Ferroviaires")
    if ga is not None and inf is not None and len(clean(ga)) and len(clean(inf)):
        ga, inf = clean(ga), clean(inf)
        crs = ga.estimate_utm_crs()
        a, b = ga.to_crs(crs), inf.to_crs(crs)
        inf_g = b[~b["type_infra"].map(lambda x: norm(x) in ("passage a niveau",))]
        for (src, tgt, sname, tname, sid) in ((a, inf_g, "Gares", "Infrastructures_Ferroviaires", "id_fichegare"),
                                             (inf_g, a, "Infrastructures_Ferroviaires", "Gares", "id_infra")):
            if not len(tgt):
                continue
            tree = STRtree(tgt.geometry.values)
            res = tree.query_nearest(src.geometry.values, max_distance=max_gare_m, all_matches=False)
            found = set(res[0].tolist())
            for pos in range(len(src)):
                if pos not in found:
                    r = src.iloc[pos]
                    rows.append((sname, r["_rid"], r["zone"], r[sid], "geometry", "XL_GARE", "WARNING",
                                 f"Aucune fiche correspondante dans {tname} à moins de {max_gare_m:g} m "
                                 "(formulaires A et I à remplir sur les mêmes gares)", ""))
    ou = layers.get("Ouvrage_franchissement")
    if ou is not None and len(clean(ou)):
        gares = [clean(x) for x in (ga, inf) if x is not None and len(clean(x))]
        vals = domains.get("Ouvrage_franchissement", {}).get("Distance à la gare la plus proche", [])
        if gares and len(vals) == 4:
            ou = clean(ou)
            ref = pd.concat([x[["geometry"]] for x in gares])
            crs = ou.estimate_utm_crs()
            o, rr = ou.to_crs(crs), gpd.GeoDataFrame(ref, geometry="geometry", crs=gares[0].crs).to_crs(crs)
            tree = STRtree(rr.geometry.values)
            idx, dists = tree.query_nearest(o.geometry.values, return_distance=True, all_matches=False)
            bins = [(0, 2000), (2000, 5000), (5000, 10000), (10000, 1e9)]
            for pos, dist in zip(idx[0], dists):
                v = o["distance_gare_proche"].iat[pos]
                n = norm(v) if v else ""
                k = next((j for j, x in enumerate(vals) if norm(x) == n), None)
                if k is None:
                    continue
                lo, hi = bins[k]
                if dist < lo * 0.8 or dist > hi * 1.2:
                    r = o.iloc[pos]
                    rows.append(("Ouvrage_franchissement", r["_rid"], r["zone"], r["id_ouvrage"], "distance_gare_proche",
                                 "XL_DIST", "WARNING",
                                 f"Distance déclarée « {v} » vs ≈ {dist/1000:.1f} km mesurés (à vol d'oiseau) à la gare la plus proche", ""))
    return pd.DataFrame(rows, columns=COLS)


# ---------------------------------------------------------------- synthèse
def run_all_qa(layers: dict, domains: dict, struct: pd.DataFrame | None = None, dup_tol_m=3.0) -> pd.DataFrame:
    parts = [run_layer_qa(g, l, domains, dup_tol_m, struct) for l, g in layers.items()]
    parts.append(run_cross_qa(layers, domains))
    out = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else pd.DataFrame(columns=COLS)
    out["severity"] = pd.Categorical(out["severity"], ["ERROR", "WARNING", "INFO"], ordered=True)
    return out.sort_values(["layer", "severity", "rule"], kind="stable").reset_index(drop=True)


def apply_status(layers: dict, issues: pd.DataFrame) -> dict:
    """Ajoute qa_status (OK/WARNING/ERROR) et qa_flags à chaque couche."""
    for name, g in layers.items():
        sub = issues[(issues.layer == name) & issues["_rid"].notna() & (issues.severity != "INFO")]
        st = sub.groupby("_rid")["severity"].apply(lambda s: "ERROR" if (s == "ERROR").any() else "WARNING")
        fl = sub.groupby("_rid")["rule"].apply(lambda s: ",".join(sorted(set(s))))
        g["qa_status"] = g["_rid"].map(st).fillna("OK")
        g["qa_flags"] = g["_rid"].map(fl).fillna("")
    return layers


def summarize(layers: dict, issues: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for name, g in layers.items():
        sub = issues[(issues.layer == name) & issues["_rid"].notna()]
        e = sub[sub.severity == "ERROR"]["_rid"].nunique()
        eset = set(sub[sub.severity == "ERROR"]["_rid"])
        w = len(set(sub[sub.severity == "WARNING"]["_rid"]) - eset)   # avertissements sans erreur
        n = len(g)
        rows.append({"Couche": name, "Entités": n, "Zones": g["zone"].nunique(dropna=True),
                     "Erreurs (entités)": e, "Avertissements seuls (entités)": w,
                     "Entités OK (%)": round(100 * (n - e - w) / n, 1) if n else 0.0,
                     "Anomalies structure": int(((issues.layer == name) & issues["_rid"].isna() & (issues.severity != "INFO")).sum())})
    return pd.DataFrame(rows)


def completeness(layers: dict) -> pd.DataFrame:
    rows = []
    for name, g in layers.items():
        for f in LAYERS[name].fields:
            rows.append({"Couche": name, "Champ": f.name, "Type": f.typ,
                         "Renseigné (%)": round(100 * (~g[f.name].map(is_null)).mean(), 1) if len(g) else 0.0})
    return pd.DataFrame(rows)
