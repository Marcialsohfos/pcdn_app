# PCDN – Collecte, fusion brute, export et rapport de traitement (Streamlit)

**FTP → lecture des .shp/.kml/.zip → reconnaissance de la couche → fusion BRUTE par couche → export GPKG / SHP / KML → rapport de traitement.**

## Principe : on ne supprime rien
* Toutes les lignes reçues sont conservées dans les exports, valeurs inchangées (copies, doublons et lignes en erreur compris).
* Seuls les **noms de colonnes** sont alignés sur le dictionnaire (indispensable pour fusionner un .kml et un .shp aux noms tronqués).
* 2 colonnes de traçabilité sont ajoutées : `fichier_source`, `ligne_source` (la colonne `fid` des KML devient `fid_source`).
* Les contrôles ne font que **signaler** ; les équipes de traitement décident.

## Résultats (`local/output/latest/`, copie datée dans `archive/AAAA-MM-JJ/`)
* `donnees_brutes/pcdn_brut.gpkg` (une couche par thème), `donnees_brutes/shp/`, `donnees_brutes/kml/`
* `rapport/rapport_traitement.xlsx` : Lisez-moi · Synthèse · **Plan_action** · **A_traiter** · **Doublons** · Champs_vides · Par_agent · Couverture_zones · Anomalies · Journal_fichiers
* `rapport/corrections_par_agent/*.xlsx` : un classeur par agent
* `suivi_quotidien.csv` : historique par jour et par couche

## Doublons
Même ID (ou nom) + même position (~1 m) = même enregistrement présent plusieurs fois.
`exact` : valeurs identiques (ex. même collecte en .kml et .shp) → on propose de ne garder qu'une ligne.
`conflit` : valeurs différentes → à arbitrer. La ligne la plus complète est recommandée ; rien n'est supprimé automatiquement.

## Installation / lancement
`pip install -r requirements.txt` puis `streamlit run app.py` · tâche planifiée : `python run_daily.py [--skip-ftp] [--formats gpkg,shp,kml]`.
Identifiants FTP : `.streamlit/secrets.toml` (Streamlit Cloud : Settings → Secrets), valeurs entre guillemets. Jamais versionnés.

## Contrôles (signalés, jamais appliqués)
Champs obligatoires · valeurs hors listes · types et plages · géométrie vide/invalide/mauvais type · hors emprise · (0,0) ·
tronçons courts · ID non conforme au format `ZONE<n><2 lettres><n>` (ex. ZONE4IN1) · ID utilisé pour plusieurs positions ·
doublons · dates futures · règles inter-champs par couche.

## Limites connues
* Shapefile : noms de champs ≤ 10 car. (correspondance dans `shp/<couche>_champs.csv`) et textes tronqués à 254 car. : le GPKG et le KML sont complets.
* KML : une ligne sans position ne peut pas y figurer (elle reste dans le GPKG et le SHP).
* `id_trocon` (faute de frappe dans la config Mapit de la couche Réseau routier) est reconnu comme `id_troncon` ; à corriger à la source.
* Streamlit Cloud : disque éphémère, les fichiers téléchargés et l'historique sont perdus au redémarrage.
