"""Contrôles de cohérence, géométrie, dates, identifiants et doublons ; chaîne complète pour une couche."""
from __future__ import annotations

import hashlib
import re

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely import STRtree

from .standardize import Issues, canonicalize
from .util import clean_na, norm_key, norm_val


# ---- aides pour les règles (colonnes booléennes "boolean" ou texte, vides = None) ---------------
def _na(s): return s.isna().to_numpy()
def _t(s): return s.astype("boolean").fillna(False).to_numpy(dtype=bool)
def _f(s): return (~s.astype("boolean")).fillna(False).to_numpy(dtype=bool)
def _blank_or(s, *vals): return s.isna().to_numpy() | s.isin(list(vals)).to_numpy()
def _nv(s): return np.array([norm_val(a) for a in s.tolist()])
def _has(s, pat): return np.array([bool(re.search(pat, a)) for a in _nv(s)])


def layer_rules(layer: str) -> list[tuple]:
    """Règles inter-champs : (gravité, type, variable, message, fonction(df) -> tableau booléen, True = anomalie)."""
    R = lambda sev, var, msg, fn: (sev, "incoherence", var, msg, fn)
    rules = {
        "Gares": [
            R("error", "type_magasin", "Présence de magasin = Oui mais type de magasin non renseigné",
              lambda d: _t(d.presence_magasin_gare) & _na(d.type_magasin)),
            R("warning", "type_magasin", "Présence de magasin = Non mais un type de magasin est renseigné",
              lambda d: _f(d.presence_magasin_gare) & ~_na(d.type_magasin)),
            R("warning", "capacite_stockage_gare", "Pas de magasin mais capacité de stockage renseignée (autre que « Non applicable »)",
              lambda d: _f(d.presence_magasin_gare) & ~_blank_or(d.capacite_stockage_gare, "Non applicable")),
        ],
        "Points_Vente_Marches": [
            R("warning", "capacite_stockage", "Pas de magasin de stockage mais capacité renseignée (autre que « Non applicable »)",
              lambda d: _f(d.presence_magasin_stockage) & ~_blank_or(d.capacite_stockage, "Non applicable")),
            R("error", "capacite_stockage", "Magasin de stockage présent mais capacité absente",
              lambda d: _t(d.presence_magasin_stockage) & _na(d.capacite_stockage)),
            R("error", "type_aire_chargement", "Aire de chargement présente mais type non renseigné",
              lambda d: _t(d.presence_aire_chargement) & _na(d.type_aire_chargement)),
            R("warning", "type_aire_chargement", "Pas d'aire de chargement mais un type est renseigné",
              lambda d: _f(d.presence_aire_chargement) & ~_na(d.type_aire_chargement)),
        ],
        "Reseau_Routier_Pistes": [
            R("error", "type_blocage", "Point critique = Oui mais aucun type de blocage",
              lambda d: _t(d.pt_critique) & _blank_or(d.type_blocage, "Aucun")),
            R("warning", "type_blocage", "Point critique = Non mais un blocage est indiqué",
              lambda d: _f(d.pt_critique) & ~_blank_or(d.type_blocage, "Aucun")),
            R("warning", "duree_coupure_jours", "Praticable toute l'année mais durée de coupure > 0",
              lambda d: (d.duree_coupure_jours.fillna(0) > 0).to_numpy() & _has(d.praticabilite_pluie, r"toute ann")),
        ],
        "Services_Sociaux_Bases": [
            R("error", "type_equipement", "Le type d'équipement ne correspond pas au domaine d'activité",
              lambda d: _equip_mismatch(d)),
        ],
        "Activites_autour_gare": [
            R("warning", "type_culture", "Type de culture renseigné alors que l'activité n'est pas agricole",
              lambda d: ~_na(d.type_culture) & ~_has(d.type_activite, r"agricult")),
            R("warning", "type_elevage", "Type d'élevage renseigné alors que l'activité n'est pas pastorale",
              lambda d: ~_na(d.type_elevage) & ~_has(d.type_activite, r"paturage|elevage")),
        ],
        "Ouvrage_franchissement": [
            R("warning", "presence_gardien", "Passage à niveau gardé mais gardien-barrières = Non",
              lambda d: _has(d.type_ouvrage, r"pn garde") & ~_has(d.type_ouvrage, r"non garde") & (_nv(d.presence_gardien) == "non")),
            R("warning", "dispositif_securite", "Passage à niveau non gardé/informel avec « gardien-barrières présent »",
              lambda d: _has(d.type_ouvrage, r"non garde|informel") & _has(d.dispositif_securite, r"gardien barrieres present")),
            R("warning", "etat_ouvrage", "Ouvrage « Impraticable » mais réhabilitation jugée non nécessaire",
              lambda d: _has(d.etat_ouvrage, r"impraticable") & _has(d.necessite_rehab, r"non necessaire")),
        ],
        "Gouvernance_Services_Securite": [
            R("warning", "moyens_mobilite", "« Aucun moyen propre » combiné avec d'autres moyens de mobilité",
              lambda d: _has(d.moyens_mobilite, r"aucun moyen propre") & d.moyens_mobilite.fillna("").str.contains(";").to_numpy()),
            R("warning", "source_energie_securite", "« Aucune » combinée avec d'autres sources d'énergie",
              lambda d: _has(d.source_energie_securite, r"(^| )aucune( |$)") & d.source_energie_securite.fillna("").str.contains(";").to_numpy()),
        ],
    }
    return rules.get(layer, [])


def _equip_mismatch(d):
    pref = np.array([norm_val(re.sub(r" [–-] .*$", "", a)) if a is not None else "" for a in d.type_equipement.tolist()])
    dom = _nv(d.domaine_activite)
    return (~_na(d.type_equipement)) & (~_na(d.domaine_activite)) & ~np.char.startswith(pref.astype(str), "autre") \
        & ~np.char.startswith(dom.astype(str), "autre") & (pref != dom)


# ---- géométrie ------------------------------------------------------------------------------
def geometry_checks(x: gpd.GeoDataFrame, layer: str, schema: dict, settings: dict) -> Issues:
    iss = Issues()
    lay = schema["layers"].set_index("layer").loc[layer]
    expected = lay["geometry"].upper()
    g = x.geometry
    empty = (g.isna() | g.is_empty).to_numpy()
    gt = np.array([("" if a is None else a.upper().replace("MULTI", "")) for a in g.geom_type.tolist()])
    iss.add(np.where(empty)[0], "error", "geometrie_vide", "geometry", None, "Géométrie absente ou illisible")
    ok = ~empty
    mism = np.where(ok & (gt != expected))[0]
    iss.add(mism, "error", "geometrie_type", "geometry", gt[mism], [f"Type de géométrie {lay['geometry']} attendu ({gt[i]} reçu)" for i in mism])
    valid = g.is_valid.to_numpy()
    iss.add(np.where(ok & ~valid)[0], "error", "geometrie_invalide", "geometry", None, "Géométrie invalide (auto-intersection, anneau non fermé…)")
    pos = np.where(ok)[0]
    if len(pos):
        cen = g.iloc[pos].centroid
        cx, cy = cen.x.to_numpy(), cen.y.to_numpy()
        bb = settings["bbox"]
        out = (cx < bb["xmin"]) | (cx > bb["xmax"]) | (cy < bb["ymin"]) | (cy > bb["ymax"])
        iss.add(pos[out], "error", "hors_emprise", "geometry", None,
                "Localisation hors de l'emprise attendue du corridor (coordonnées inversées ou erronées ?)")
        iss.add(pos[(cx == 0) & (cy == 0)], "error", "hors_emprise", "geometry", "0,0", "Coordonnées (0,0) : GPS non capté")
    if expected == "LINESTRING":
        idx = np.where(ok & np.array([("LINESTRING" in a) for a in gt]))[0]
        if len(idx):
            ln = gpd.GeoSeries(g.iloc[idx].values, crs=4326).to_crs(32633).length.to_numpy()
            short = ln < settings["min_line_length_m"]
            iss.add(idx[short], "warning", "tronçon_court", "geometry", np.round(ln[short], 1),
                    [f"Tronçon de {a:.1f} m seulement (< {settings['min_line_length_m']} m)" for a in ln[short]])
    if expected == "POINT" and ok.sum() > 1:
        idx = np.where(ok)[0]
        pts = gpd.GeoSeries(g.iloc[idx].values, crs=4326).to_crs(32633).values
        id_var, nm_var = lay["id_var"], lay["name_var"]
        nmv = x[nm_var].tolist() if isinstance(nm_var, str) and nm_var in x.columns else [None] * len(x)
        idv = x[id_var].tolist()
        key = [norm_val(f"{idv[i] or ''} {nmv[i] or ''}") for i in idx]       # sans id ni nom : pas de comparaison
        tree = STRtree(pts)
        a, b = tree.query(pts, predicate="dwithin", distance=settings["dup_point_tolerance_m"])
        flagged = {int(i) for i, j in zip(a, b) if i != j and key[i] and key[i] == key[j]}
        iss.add(sorted(idx[list(flagged)]), "warning", "doublon_spatial", "geometry", None,
                f"Point à moins de {settings['dup_point_tolerance_m']} m d'un autre point identique (même id/nom)")
    return iss


def date_checks(x: pd.DataFrame, settings: dict) -> Issues:
    iss, d = Issues(), x["_date"]
    today = pd.Timestamp.today().normalize()
    fut = np.where((d > today + pd.Timedelta(days=1)).fillna(False).to_numpy())[0]
    iss.add(fut, "error", "date_future", "_date", [d.iloc[i].date().isoformat() for i in fut],
            "Date de collecte dans le futur (horloge du terminal mal réglée ?)")
    sd = settings.get("start_date")
    if sd is not None and not pd.isna(sd):
        ant = np.where((d < pd.Timestamp(sd)).fillna(False).to_numpy())[0]
        iss.add(ant, "warning", "date_anterieure", "_date", [d.iloc[i].date().isoformat() for i in ant],
                f"Date antérieure au début de la collecte ({pd.Timestamp(sd).date()})")
    return iss


def _parse_dates(s: pd.Series) -> pd.Series:
    iso = pd.to_datetime(s, errors="coerce", format="ISO8601")
    rest = iso.isna() & s.notna()
    if rest.any():
        iso[rest] = pd.to_datetime(s[rest], errors="coerce", dayfirst=True, format="mixed")
    return iso.dt.tz_localize(None) if getattr(iso.dt, "tz", None) is not None else iso


def add_meta(x: gpd.GeoDataFrame, settings: dict) -> gpd.GeoDataFrame:
    """Colonnes _date (date de collecte, sinon date du fichier) et _agent d'après les colonnes optionnelles reconnues."""
    keys = {norm_key(c): c for c in x.columns}

    def pick(cands):
        for c in cands:
            if c in keys:
                return x[keys[c]]
        return pd.Series([None] * len(x), index=x.index, dtype="object")

    dt = _parse_dates(pd.Series(pick(settings["date_cols"]).tolist(), index=x.index, dtype="object"))
    fd = pd.to_datetime(x["_file_date"])
    x["_date"] = dt.dt.normalize().where(dt.notna(), fd)
    ag = clean_na(pick(settings["agent_cols"]))
    x["_agent"] = pd.Series([a if a is not None else "INCONNU" for a in ag.tolist()], index=x.index, dtype="object")
    return x


def id_checks(x, layer, schema, settings) -> Issues:
    iss = Issues()
    id_var = schema["layers"].set_index("layer").loc[layer, "id_var"]
    ids = x[id_var]
    dup = np.where(ids.notna() & (ids.duplicated(keep=False)))[0]
    iss.add(dup, "error", "id_duplique", id_var, ids.iloc[dup].tolist(), [f"Identifiant « {ids.iloc[i]} » utilisé plusieurs fois dans la couche" for i in dup])
    pat = settings.get("id_pattern")
    if not pat and settings.get("id_convention", True):               # convention : ZONE<n> + 2 premières lettres de la couche + <n>
        pref = layer[:2].upper()
        pat = rf"^ZONE\d+{pref}\d+$"
        label = f"ZONE<n>{pref}<n>"
    else:
        label = pat
    if pat:
        bad = [i for i, a in enumerate(ids.tolist()) if a is not None and not re.search(pat, a)]
        iss.add(bad, "error", "id_format", id_var, [ids.iloc[i] for i in bad], [f"Identifiant « {ids.iloc[i]} » non conforme au format attendu ({label})" for i in bad])
    return iss


# ---- chaîne complète pour UNE couche ------------------------------------------------------------
def process_layer(x: gpd.GeoDataFrame, layer: str, schema: dict, settings: dict, log):
    lay = schema["layers"].set_index("layer").loc[layer]
    id_var = lay["id_var"]
    nm_var = lay["name_var"] if isinstance(lay["name_var"], str) and lay["name_var"] else None
    variables = schema["vars"].loc[schema["vars"]["layer"] == layer, "variable"].tolist()
    x = x.reset_index(drop=True)
    # 1) dédoublonnage exact entre fichiers (même enregistrement renvoyé plusieurs jours)
    attrs = [c for c in x.columns if c not in ("geometry", "_source", "_file_date")]
    wkt = x.geometry.to_wkt(rounding_precision=5).tolist()     # ~1 m : même enregistrement relu en .kml et .shp
    sig = [hashlib.md5("\x1f".join([str(v) for v in row] + [w]).encode()).hexdigest()
           for row, w in zip(x[attrs].itertuples(index=False, name=None), wkt)]
    x = x.assign(_sig=sig).sort_values("_file_date", ascending=False, kind="stable").drop_duplicates("_sig").drop(columns="_sig")
    x = add_meta(x.reset_index(drop=True), settings)
    # même collecte exportée en .kml ET .shp : même id + même position (~10 cm) -> on garde la plus complète
    if id_var in x.columns:
        cen = x.geometry.centroid
        pos = [f"{round(a, 5)} {round(b, 5)}" if a == a else "" for a, b in zip(cen.x, cen.y)]   # ~1 m
        names = x[nm_var].tolist() if nm_var and nm_var in x.columns else [None] * len(x)
        # clé = identifiant (sinon nom) + position : repère le même enregistrement relu en .kml et .shp
        x["_k"] = [None if (i is None and n is None) else f"{i if i is not None else 'nom:' + str(n)} {p}"
                   for i, n, p in zip(x[id_var].tolist(), names, pos)]
        x["_comp"] = x[variables].notna().sum(axis=1)
        n0 = len(x)
        x = x.sort_values(["_comp", "_file_date"], ascending=False, kind="stable")
        x = x[x["_k"].isna() | ~x["_k"].duplicated()].drop(columns=["_k", "_comp"])
        if len(x) < n0:
            log(f"{layer} : {n0 - len(x)} doublon(s) kml/shp fusionné(s)")
    # 2) tri : version la plus récente d'abord
    x = x.sort_values(["_date", "_file_date"], ascending=False, kind="stable").reset_index(drop=True)
    x["_row"] = np.arange(len(x))
    cz, iss = canonicalize(x, layer, schema)
    frames = [iss.frame()]
    for part in (id_checks(cz, layer, schema, settings), date_checks(cz, settings), geometry_checks(cz, layer, schema, settings)):
        frames.append(part.frame())
    rules = Issues()
    plain = pd.DataFrame(cz.drop(columns="geometry"))
    for sev, typ, var, msg, fn in layer_rules(layer):
        try:
            flag = np.asarray(fn(plain), dtype=bool)
        except Exception as e:
            log(f"Règle ignorée ({layer}) : {e}", level="WARN")
            continue
        rules.add(np.where(flag)[0], sev, typ, var, None, msg)
    frames.append(rules.frame())
    allis = pd.concat([f for f in frames if len(f)], ignore_index=True) if any(len(f) for f in frames) else \
        pd.DataFrame(columns=["row", "severity", "type", "variable", "value", "message"])
    ctx = pd.DataFrame({"row": cz["_row"].to_numpy(), "layer": layer, "id": cz[id_var].to_numpy(),
                        "nom": cz[nm_var].to_numpy() if nm_var else None, "agent": cz["_agent"].to_numpy(),
                        "date": cz["_date"].dt.strftime("%Y-%m-%d").to_numpy(), "fichier": cz["_source"].to_numpy()})
    iss_df = allis.merge(ctx, on="row", how="left").drop(columns="row")
    iss_df = iss_df[["layer", "severity", "type", "id", "nom", "agent", "date", "variable", "value", "message", "fichier"]]
    iss_df = iss_df.assign(_e=(iss_df["severity"] != "error")).sort_values(["_e", "agent", "id"], kind="stable").drop(columns="_e").reset_index(drop=True)
    n = len(cz)
    cz["_n_err"] = np.bincount(allis.loc[allis["severity"] == "error", "row"].astype(int), minlength=n)[:n]
    cz["_n_warn"] = np.bincount(allis.loc[allis["severity"] == "warning", "row"].astype(int), minlength=n)[:n]
    return cz.drop(columns="_row"), iss_df
