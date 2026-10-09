"""Exécution planifiée (cron / planificateur Windows) :  python run_daily.py [--skip-ftp] [--formats gpkg,xlsx,shp,kml]"""
import sys
from datetime import date

from pcdn import ftp
from pcdn.exporters import export_all
from pcdn.pipeline import run_pipeline
from pcdn.settings import get_settings, load_schema
from pcdn.util import Logger

args = sys.argv[1:]
S = get_settings()
log = Logger(S["dir_logs"] / f"run_{date.today():%Y%m%d}.log", echo=True)
log("===== Démarrage du monitoring PCDN =====")
schema, status = load_schema(S["config_dir"]), 0
if "--skip-ftp" not in args:
    try:
        ftp.sync(S, log)
    except Exception as e:
        log(f"FTP indisponible : {e} -> traitement des fichiers déjà présents", level="ERROR")
        status = 2
fmts = ["gpkg", "xlsx"] + (args[args.index("--formats") + 1].split(",") if "--formats" in args else [])
res = run_pipeline(S, schema, log)
if res["data"]:
    export_all(res, schema, S, log, formats=fmts)
    iss = res["issues_all"]
    log(f"Terminé : {len(res['data'])} couche(s), {(iss.severity == 'error').sum()} erreur(s), {(iss.severity == 'warning').sum()} avertissement(s)")
else:
    log("Aucune donnée exploitable trouvée", level="WARN")
    status = max(status, 1)
sys.exit(status)
