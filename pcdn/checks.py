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
        grp = x["_grp"].to_numpy()[idx] if "_grp" in x.columns else np.full(len(idx), -1)
        flagged = {int(i) for i, j in zip(a, b) if i != j and key[i] and key[i] == key[j] and not (grp[i] >= 0 and grp[i] == grp[j])}
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
    ids = x[id_var].tolist()
    d = pd.DataFrame({"i": ids, "p": x["_pos"].tolist()})
    d = d[d["i"].notna()]
    nun = d.groupby("i")["p"].nunique()
    bad_ids = set(nun[nun > 1].index)                      # même ID sur des positions différentes (les copies ne comptent pas)
    dup = [r for r, a in enumerate(ids) if a is not None and a in bad_ids]
    iss.add(dup, "error", "id_duplique", id_var, [ids[i] for i in dup],
            [f"Identifiant « {ids[i]} » utilisé pour plusieurs positions différentes" for i in dup])
    pat = settings.get("id_pattern")
    if not pat and settings.get("id_convention", True):    # ZONE<n> + 2 premières lettres de la couche + <n>
        pref = layer[:2].upper()
        pat, label = rf"^ZONE\d+{pref}\d+$", f"ZONE<n>{pref}<n>"
    else:
        label = pat
    if pat:
        bad = [i for i, a in enumerate(ids) if a is not None and not re.search(pat, a)]
        iss.add(bad, "error", "id_format", id_var, [ids[i] for i in bad],
                [f"Identifiant « {ids[i]} » non conforme au format attendu ({label})" for i in bad])
    return iss


def _cmp(v):
    """Valeur comparable (kml/shp écrivent 'true'/'True', '5'/'5.0' différemment)."""
    if v is None:
        return ""
    try:
        return repr(float(str(v).replace(",", ".")))
    except ValueError:
        return norm_val(v)


def duplicate_groups(x: gpd.GeoDataFrame, variables: list[str], id_var: str, nm_var: str | None):
    """Repère (sans rien supprimer) les enregistrements présents plusieurs fois : même ID (ou nom) + même position (~1 m).
    -> groupe, nature ('exact' = valeurs identiques / 'conflit' = valeurs différentes), conserver (ligne la plus complète)."""
    n = len(x)
    ids = x[id_var].tolist() if id_var in x.columns else [None] * n
    nms = x[nm_var].tolist() if nm_var and nm_var in x.columns else [None] * n
    pos = x["_pos"].tolist()
    buckets: dict[str, list[int]] = {}
    for r in range(n):
        if ids[r] is None and nms[r] is None:
            continue
        buckets.setdefault(f"{ids[r] if ids[r] is not None else 'nom:' + str(nms[r])} {pos[r]}", []).append(r)
    sig = [tuple(_cmp(v) for v in row) for row in x[variables].itertuples(index=False, name=None)]
    comp = x[variables].notna().sum(axis=1).tolist()
    fdate = pd.to_datetime(x["_file_date"]).tolist()
    grp, nature, keep = np.full(n, -1), [""] * n, np.zeros(n, dtype=bool)
    g = 0
    for rows in buckets.values():
        if len(rows) < 2:
            continue
        kind = "exact" if len({sig[r] for r in rows}) == 1 else "conflit"
        best = sorted(rows, key=lambda r: (-comp[r], -fdate[r].value if pd.notna(fdate[r]) else 0, r))[0]
        for r in rows:
            grp[r], nature[r], keep[r] = g, kind, (r == best)
        g += 1
    return grp, nature, keep


def qa_layer(raw: gpd.GeoDataFrame, layer: str, schema: dict, settings: dict, log):
    """Contrôle une couche SANS modifier ni supprimer de ligne. -> (statut par ligne, anomalies)."""
    from .standardize import harmonize
    lay = schema["layers"].set_index("layer").loc[layer]
    id_var = lay["id_var"]
    nm_var = lay["name_var"] if isinstance(lay["name_var"], str) and lay["name_var"] else None
    variables = schema["vars"].loc[schema["vars"]["layer"] == layer, "variable"].tolist()
    x = raw.reset_index(drop=True)
    h, _, _ = harmonize(x, layer, schema)               # copie de travail (nettoyée) : les données brutes ne sont pas touchées
    h = add_meta(h, settings)
    h["_row"] = np.arange(len(h))
    cen = h.geometry.centroid
    h["_pos"] = [f"{round(a, 5)} {round(b, 5)}" if a == a else "" for a, b in zip(cen.x, cen.y)]
    grp, nature, keep = duplicate_groups(h, variables, id_var, nm_var)
    h["_grp"] = grp
    cz, iss = canonicalize(h, layer, schema)
    parts = [iss.frame()]
    for part in (id_checks(cz, layer, schema, settings), date_checks(cz, settings), geometry_checks(cz, layer, schema, settings)):
        parts.append(part.frame())
    # doublons d'enregistrement : signalés, jamais supprimés
    dup, reco = Issues(), [""] * len(cz)
    src, lig = cz["_source"].tolist(), cz["_row_src"].tolist()
    for gnum in sorted(set(grp[grp >= 0])):
        rows = [int(r) for r in np.where(grp == gnum)[0]]
        k = next(r for r in rows if keep[r])
        for r in rows:
            if nature[r] == "exact":
                if r == k:
                    reco[r] = "Conserver (ligne la plus complète du groupe)"
                else:
                    reco[r] = "Copie identique : à écarter après validation"
                    dup.add([r], "warning", "doublon_exact", id_var, None,
                            f"Copie identique de la ligne {lig[k]} de « {src[k]} » (ex. même collecte en .kml et .shp)")
            else:
                reco[r] = "Valeurs différentes : arbitrer" + (" (version la plus complète)" if r == k else "")
                dup.add([r], "warning", "doublon_conflit", id_var, None,
                        f"Mêmes ID/position que la ligne {lig[k] if r != k else [lig[q] for q in rows if q != r][0]} de « "
                        f"{src[k] if r != k else [src[q] for q in rows if q != r][0]} » mais valeurs différentes : à arbitrer")
    parts.append(dup.frame())
    rules, plain = Issues(), pd.DataFrame(cz.drop(columns="geometry"))
    for sev, typ, var, msg, fn in layer_rules(layer):
        try:
            flag = np.asarray(fn(plain), dtype=bool)
        except Exception as e:
            log(f"Règle ignorée ({layer}) : {e}", level="WARN")
            continue
        rules.add(np.where(flag)[0], sev, typ, var, None, msg)
    parts.append(rules.frame())
    cols = ["row", "severity", "type", "variable", "value", "message"]
    allis = pd.concat([f for f in parts if len(f)], ignore_index=True) if any(len(f) for f in parts) else pd.DataFrame(columns=cols)
    n = len(cz)
    ctx = pd.DataFrame({"row": np.arange(n), "layer": layer, "id": cz[id_var].to_numpy(),
                        "nom": cz[nm_var].to_numpy() if nm_var else None, "agent": cz["_agent"].to_numpy(),
                        "date": cz["_date"].dt.strftime("%Y-%m-%d").to_numpy(),
                        "fichier": cz["_source"].to_numpy(), "ligne": cz["_row_src"].to_numpy()})
    iss_df = allis.merge(ctx, on="row", how="left")
    iss_df = iss_df[["layer", "severity", "type", "id", "nom", "agent", "date", "variable", "value", "message", "fichier", "ligne", "row"]]
    iss_df = iss_df.assign(_e=(iss_df["severity"] != "error")).sort_values(["_e", "agent", "id"], kind="stable").drop(columns="_e").reset_index(drop=True)
    n_err = np.bincount(allis.loc[allis["severity"] == "error", "row"].astype(int), minlength=n)[:n]
    n_warn = np.bincount(allis.loc[allis["severity"] == "warning", "row"].astype(int), minlength=n)[:n]
    prob = allis.groupby("row")["message"].apply(lambda m: " | ".join(dict.fromkeys(m)))
    rows_df = pd.DataFrame({
        "couche": layer, "fichier_source": src, "ligne_source": lig, "id": cz[id_var].to_numpy(),
        "nom": cz[nm_var].to_numpy() if nm_var else None, "agent": cz["_agent"].to_numpy(), "date": ctx["date"].to_numpy(),
        "format": cz["_fmt"].to_numpy(), "n_err": n_err, "n_warn": n_warn,
        "statut": np.where(n_err > 0, "À corriger", np.where(n_warn > 0, "À vérifier", "Conforme")),
        "problemes": [prob.get(i, "") for i in range(n)],
        "groupe_doublon": [int(g) if g >= 0 else None for g in grp], "nature_doublon": nature, "recommandation_doublon": reco,
        "longitude": np.round(cen.x.to_numpy(), 6), "latitude": np.round(cen.y.to_numpy(), 6)})
    return rows_df, iss_df.drop(columns="row")
