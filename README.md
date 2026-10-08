# PCDN Corridor — pipeline cartographique (Streamlit)

FTP/FTPS → lecture SHP/KML → fusion par couche → assurance qualité → pré-traitement → export GPKG / SHP / KML.

## Lancer
```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # puis renseigner l'hôte, l'utilisateur, le mot de passe
streamlit run app.py
```
## Tests
```bash
python tests/test_pipeline.py   # chaîne complète sur données synthétiques
python tests/test_ftp.py        # client FTP contre un serveur local (pip install pyftpdlib)
python tests/test_app.py        # démarrage de l'UI
```
## Organisation
- `pcdn/config.py` : schéma des 10 couches (dictionnaire .docx) ; domaines lus dans `dictionaries/*.json`
- `pcdn/sources.py` : FTP/FTPS, découverte, détection couche/zone (nom de fichier ou dossier : `Zone 2`, `ZONE2`…)
- `pcdn/readers.py` : SHP/KML/KMZ, reconnaissance des champs tronqués à 10 car., fusion
- `pcdn/qa.py` : contrôles ; `pcdn/preprocess.py` : corrections ; `pcdn/exporters.py` : exports

## Contrôles QA (code → sens)
STRUCT_* structure · GEOM_NULL/TYPE/INVALID/ZERO/BBOX/DUP · LINE_SHORT · ID_MISSING/FORMAT/PREFIX/ZONE/DUP/GAP ·
REQ_MISSING · DOM_INVALID/DOM_CASE (valeur hors dictionnaire) · TYPE_INVALID/DECIMAL · RANGE · MULTI_EXCL ·
L_* règles de cohérence métier · XL_GARE / XL_DIST contrôles inter-couches.

## Ajouter / modifier une règle ou une couche
Éditer `pcdn/config.py` (champs) et `LOGIC` dans `pcdn/qa.py`. Déposer le JSON de domaine dans `dictionaries/<Couche>.json`.
