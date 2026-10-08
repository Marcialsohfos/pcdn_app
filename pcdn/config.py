"""Schéma des 10 couches PCDN (d'après le Dictionnaire de données) et constantes."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

DICT_DIR = Path(__file__).resolve().parent.parent / "dictionaries"
CRS_OUT = "EPSG:4326"
BBOX = (8.3, 1.6, 16.3, 13.2)  # lon_min, lat_min, lon_max, lat_max (Cameroun, marge incluse)
# ID harmonisé : ZONE<n> + 2 premières lettres de la couche + n° (ex. ZONE4IN1)
ID_RE = re.compile(r"^ZONE([1-9]\d*)([A-Z]{2})([1-9]\d*)$")
META_COLS = ["zone", "source_file", "source_fmt"]


@dataclass(frozen=True)
class F:
    name: str
    typ: str = "TEXT"          # TEXT | MULTISELECT | BOOLEAN | INTEGER | DOUBLE | PICTURE
    dom: Optional[str] = None  # nom du champ dans le JSON de dictionnaire
    req: Optional[str] = None  # 'E' = obligatoire (erreur) ; 'W' = recommandé (avertissement)


@dataclass
class Layer:
    name: str
    geom: str                  # Point | LineString
    fields: list = field(default_factory=list)
    aliases: tuple = ()

    @property
    def prefix(self) -> str:
        return self.name[:2].upper()

    @property
    def id_field(self) -> str:
        return self.fields[0].name

    def f(self, name: str) -> F:
        return next(x for x in self.fields if x.name == name)


T, M, B, I, D, P = "TEXT", "MULTISELECT", "BOOLEAN", "INTEGER", "DOUBLE", "PICTURE"
TEL = F("reseau_telecom", T, "Réseau de télécommunication")

_L = [
    Layer("Appui_Production_Valorisation", "Point", [
        F("id_infra_prod", T, req="E"), F("nom_site", T, req="W"),
        F("type_unite", T, "Type_Infrastructure_Socio_Eco", "W"),
        F("etat_fonct", T, "Etat_Fonctionnement"),
        F("capacite_stockage_t", D), F("connexion_corridor", T, "Connexion_Corridor")],
        ("appui_production",)),
    Layer("Gouvernance_Services_Securite", "Point", [
        F("id_poste", T, req="E"), F("nom_poste", T, req="W"), F("zone_competence"), F("observations"),
        F("type_service", T, "Type de service", "W"), F("niveau_hierarchique", T, "Niveau hiérarchique"),
        F("etat_fonct_securite", T, "État de fonctionnement"), F("effectif_estime", T, "Effectif estimé"),
        F("moyens_mobilite", M, "Moyens de mobilité disponibles"),
        F("moyens_communication", M, "Moyens de communication"),
        F("distance_acces_corridor", T, "Distance / temps d'accès au corridor (gare ou voie)"),
        F("nature_interventions", M, "Nature des interventions courantes"),
        F("frequence_interventions", T, "Fréquence des interventions sur le corridor"),
        F("existence_convention", T, "Existence de convention / protocole avec l'exploitant ferroviaire"),
        F("source_energie_securite", M, "Source d'énergie"), TEL],
        ("service_securite", "gouvernance")),
    Layer("Ground_Truthing_LandCover", "Point", [
        F("id_point_gt", T, req="E"), F("classe_reelle", T, "Classe_Occupation_Sol", "E"),
        F("densite_couvert", I), F("observation_env"),
        F("photo_nord", P, req="W"), F("photo_sud", P, req="W")],
        ("ground_truthing", "landcover")),
    Layer("Infrastructures_Ferroviaires", "Point", [
        F("id_infra", T, req="E"), F("nom_gare", T, req="W"),
        F("troncon", T, "Troncon_Ferroviaire", "W"), F("type_infra", T, "Type_Infrastructure_Ferroviaire", "W"),
        F("etat_fonct", T, "Etat_Fonctionnement"), F("quai_embarq", B), F("zone_stockage", B),
        F("connexion_route", T, "Connexion_Route_Gare"), F("mode_apport", T, "Mode_Apport"),
        F("temps_acces_min", I), F("photo_ref", P)],
        ("infra_ferro",)),
    Layer("Points_Vente_Marches", "Point", [
        F("id_marche", T, req="E"), F("nom_marche", T, req="W"), F("zone_desservie"),
        F("zone_implantation", T, "Zone d'implantation"), F("type_marche", T, "Type"),
        F("type_marchandise", M, "Type de marchandise"), F("origine_flux", M, "Origine flux"),
        F("presence_magasin_stockage", B), F("capacite_stockage", T, "Capacité de stockage"),
        F("presence_aire_chargement", B), F("type_aire_chargement", T, "Type d'aire de chargement"),
        F("distance_gare_route", T, "Distance à la gare/route la plus proche"),
        F("frequence_activite", T, "Fréquence d'activité"), TEL],
        ("point_de_vente_marche", "points_vente", "point_vente")),
    Layer("Reseau_Routier_Pistes", "LineString", [
        F("id_troncon", T, req="E"), F("nom_voie", T, req="W"),
        F("categorie_voie", T, "Categorie_Voie", "W"), F("type_surface", T, "Type_Revetement"),
        F("largeur_m", D), F("vitesse_moy_kmh", D), F("pt_critique", B),
        F("type_blocage", T, "Type_Blocage"), F("praticabilite_pluie", T, "Praticabilite_Saison_Pluies"),
        F("duree_coupure_jours", I)],
        ("reseau_routier",)),
    Layer("Services_Sociaux_Bases", "Point", [
        F("id_service", T, req="E"), F("nom_etablissement", T, req="W"), F("zone_desservie"),
        F("domaine_activite", T, "Type de service / Domaine d'activité", "W"),
        F("type_equipement", T, "Type équipement"), F("etat_fonct_service", T, "État de fonctionnement"),
        F("source_energie_service", M, "Source d'énergie"),
        F("temps_acces_corridor", T, "Temps d'accès au corridor"),
        F("accessibilite", T, "Accessibilité"), F("capacite_accueil", T, "Capacité d'accueil"), TEL],
        ("services_sociaux",)),
    Layer("Activites_autour_gare", "Point", [
        F("id_activite", T, req="E"), F("nom_designation", T, req="W"), F("observations"),
        F("type_activite", T, "Type d'activité", "W"), F("type_culture", M, "Type de culture"),
        F("type_elevage", M, "Type d'élevage / usage pastoral"),
        F("localisation_gare", T, "Localisation par rapport à la gare/voie"),
        F("distance_voie", T, "Distance à la voie ferrée"), F("statut_occupation", T, "Statut d'occupation"),
        F("nature_foncier", T, "Nature du foncier occupé"), F("superficie_estimee", T, "Superficie estimée"),
        F("traversee_betail", T, "Traversée du bétail sur la voie"),
        F("nb_exploitants", T, "Nombre d'exploitants / acteurs estimé"),
        F("genre_exploitants", T, "Genre des exploitants (dominant)"),
        F("anciennete_activite", T, "Ancienneté de l'activité"), F("point_traversee_informelle", B),
        F("niveau_risque", T, "Niveau de risque sécuritaire"),
        F("impact_exploitation", T, "Impact sur l'exploitation ferroviaire"),
        F("acces_services_base", M, "Accès aux services de base sur site"), TEL],
        ("activite_autour_gare",)),
    Layer("Gares", "Point", [
        F("id_fichegare", T, req="E"), F("nom_gare_detail", T, req="W"), F("zone_desservie"),
        F("axe_ferroviaire", T, "Axe"), F("type_gare", T, "Type de gare"),
        F("etat_fonct_garedet", T, "Etat fonctionnement"), F("quai_embarquement", T, "Quai d'embarquement"),
        F("connexion_route_gare", T, "Connexion route"),
        F("mode_acces_gare", M, "Mode d'accès à la gare (Apport)"), F("temps_acces_garedet", T, "Temps mis"),
        F("distance_approx", T, "Distance approximative"), F("presence_magasin_gare", B),
        F("type_magasin", T, "Type de magasin"), F("capacite_stockage_gare", T, "Capacité de stockage"),
        TEL, F("operateur_telecom", M, "Opérateur de télécommunication")],
        ("gares",)),
    Layer("Ouvrage_franchissement", "Point", [
        F("id_ouvrage", T, req="E"), F("nom_repere", T, req="W"), F("zone_desservie"), F("observations"),
        F("type_ouvrage", T, "Type d'ouvrage", "W"), F("statut_ouvrage", T, "Statut / caractère officiel"),
        F("usage_principal", M, "Usage principal"), F("etat_ouvrage", T, "État de l'ouvrage"),
        F("dispositif_securite", M, "Dispositif de sécurité"),
        F("presence_gardien", T, "Présence de gardien-barrières"),
        F("visibilite_ouvrage", T, "Visibilité de l'ouvrage"),
        F("frequence_circulation", T, "Fréquence de circulation (route)"),
        F("historique_accidents", T, "Historique d'accidents signalés"),
        F("connexion_route_classee", T, "Connexion à une route classée"),
        F("distance_gare_proche", T, "Distance à la gare la plus proche"),
        F("necessite_rehab", T, "Nécessité de réhabilitation/aménagement")],
        ("ouvrage",)),
]
LAYERS: dict[str, Layer] = {l.name: l for l in _L}


@lru_cache(maxsize=4)
def load_domains(path: str = str(DICT_DIR)) -> dict:
    """{couche: {nom_champ_json: [valeurs]}} lu depuis dictionaries/<couche>.json"""
    out: dict = {}
    for name in LAYERS:
        p = Path(path) / f"{name}.json"
        out[name] = {}
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            out[name] = {f["name"]: f.get("values", []) for f in data.get("fields", [])}
    return out


def check_spec(domains: dict) -> list[str]:
    """Contrôle de cohérence schéma ↔ dictionnaires JSON (retourne les anomalies)."""
    probs = []
    for l in LAYERS.values():
        if not domains.get(l.name):
            probs.append(f"Dictionnaire JSON absent pour {l.name}")
            continue
        for f in l.fields:
            if f.dom and f.dom not in domains[l.name]:
                probs.append(f"{l.name}.{f.name}: champ JSON '{f.dom}' introuvable")
    return probs
