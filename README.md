# PCDN – Monitoring des données de terrain (Streamlit)

Portage Python/Streamlit du pipeline R `pcdn_monitoring_project` : **FTP → lecture des .shp/.kml/.zip → reconnaissance
automatique de la couche → contrôle qualité → consolidation → exports**, avec suivi quotidien et corrections par agent.

## Installation
```bash
pip install -r requirements.txt
```
Identifiants FTP : copier `.streamlit/secrets.toml.example` en `.streamlit/secrets.toml` (ou définir les variables
d'environnement `PCDN_FTP_HOST`, `PCDN_FTP_USER`, `PCDN_FTP_PWD`, `PCDN_FTP_PROTO`, `PCDN_FTP_PATH`). Ne jamais les versionner.

## Utilisation
* Interface : `streamlit run app.py` (onglets Collecte → Traitement → Tableau de bord → Anomalies → Données & carte → Export).
* Planifié (cron / planificateur Windows) : `python run_daily.py [--skip-ftp] [--formats shp,kml]`.

## Résultats (`local/output/latest/`, copie datée dans `archive/AAAA-MM-JJ/`)
`pcdn_donnees.gpkg` · `pcdn_donnees.xlsx` · `controle_qualite.xlsx` (Resume, Par_type_anomalie, Par_agent, Anomalies,
Journal_fichiers) · `corrections_par_agent/*.xlsx` · `shp/` · `kml/` · `../suivi_quotidien.csv`.

## Contrôles
Champs obligatoires · valeurs hors listes (casse/accents/tirets tolérés, multi-choix) · types et plages · Oui/Non ·
géométrie vide/invalide/mauvais type · hors emprise · (0,0) · tronçons courts · identifiants dupliqués ·
**format d'ID `ZONE<n><2 lettres de la couche><n>`** (ex. `ZONE4IN1`) · points doublons · dates futures · règles inter-champs par couche.

## Différences avec la version R
* Coordonnées KML « lon, lat,alt » (espace après la virgule) désormais lues correctement (en R elles donnaient des géométries vides).
* Guillemets enveloppant les valeurs des KML Mapit (`"ZONE4GR1"`) retirés.
* Doublon .kml/.shp : clé = identifiant, **à défaut le nom**, + position (~1 m).
* Règles « Ouvrage_franchissement » : motifs corrigés (`pn garde` / `non garde`).
* Règles de format d'ID par couche (convention ZONE) activées par défaut (`id_convention` dans `pcdn/settings.py`).

## Adapter
`pcdn/settings.py` (emprise, date de début, colonnes agent/date, seuils) · `config/pcdn_*.csv` (schéma et listes) ·
`pcdn/checks.py` → `layer_rules()` (règles inter-champs).
Sur Streamlit Cloud le disque est éphémère : les fichiers téléchargés et l'historique sont perdus au redémarrage ;
pour un suivi durable, héberger l'app sur un serveur avec disque persistant.
