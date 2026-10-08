"""Test de bout en bout avec données synthétiques : python tests/test_pipeline.py"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point

from pcdn import exporters, qa, readers
from pcdn.config import LAYERS, check_spec, load_domains
from pcdn.preprocess import PreOpts, preprocess_all, quarantine
from pcdn.sources import Resolved, build_sources, detect_layer, detect_zone, fetch


def make(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    # Gares ZONE1 : 1 OK, 1 casse/ID minuscule, 1 hors Cameroun, 1 doublon d'ID, magasin incohérent
    g = gpd.GeoDataFrame({
        "id_fichegare": ["ZONE1GA1", "zone1-ga-2", "ZONE1GA1", "ZONE1GA4"],
        "nom_gare_detail": ["Bélabo", "Nanga", "Obala", None],
        "axe_ferroviaire": ["Yaoundé-Ngaoundéré", "yaoundé-ngaoundéré", "Douala-Yaoundé", "Axe inconnu"],
        "presence_magasin_gare": ["Non", "Oui", "oui", "Non"],
        "type_magasin": ["Boutique-commerce", None, "Entrepôt frigorifique", None],
        "mode_acces_gare": ["A pied; Moto-taxi", "a pied, vélo", "Moto-taxi", "Hélicoptère"],
        "reseau_telecom": ["Bonne couverture"] * 4,
    }, geometry=[Point(13.3, 4.9), Point(11.1, 4.4), Point(4.9, 13.3), Point(0, 0)], crs=4326)
    g.to_file(root / "ZONE1_Gares.shp", encoding="UTF-8")      # noms > 10 car. tronqués par le shapefile
    # Infrastructures ZONE1 en KML seul
    kml = ('<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
           '<Placemark><name>ZONE1IN1</name><ExtendedData><Data name="id_infra"><value>ZONE1IN1</value></Data>'
           '<Data name="nom_gare"><value>Belabo</value></Data><Data name="type_infra"><value>Gare voyageurs</value></Data>'
           '<Data name="quai_embarq"><value>true</value></Data><Data name="temps_acces_min"><value>abc</value></Data></ExtendedData>'
           '<Point><coordinates>13.3001,4.9001,0</coordinates></Point></Placemark></Document></kml>')
    (root / "Zone2_Infrastructures_Ferroviaires.kml").write_text(kml, encoding="utf-8")
    # Réseau routier ZONE4
    r = gpd.GeoDataFrame({"id_troncon": ["ZONE4RE1", "ZONE4RE3"], "nom_voie": ["RN1", "P12"],
                          "categorie_voie": ["Route Nationale (RN)", "Piste informelle"],
                          "type_surface": ["Terre", "Latérite"], "pt_critique": [True, False],
                          "type_blocage": ["Aucun", "Pont endommagé"], "largeur_m": [6.5, 80]},
                         geometry=[LineString([(13.0, 5.0), (13.1, 5.1)]), LineString([(13.2, 5.2), (13.2001, 5.2)])], crs=4326)
    r.to_file(root / "ZONE4" / "Reseau_Routier_Pistes.shp" if (root / "ZONE4").mkdir(exist_ok=True) is None else None, encoding="UTF-8")


def main():
    doms = load_domains()
    probs = check_spec(doms)
    assert not probs, probs
    assert detect_layer("ZONE1_Gares.shp") == "Gares" and detect_layer("x/Activites_autour_gare.kml") == "Activites_autour_gare"
    assert detect_zone("/FTP/Zone 04/a.shp") == "ZONE4"

    td = Path(tempfile.mkdtemp())
    make(td)
    entries = [(str(p.relative_to(td)).replace("\\", "/"), p.stat().st_size) for p in td.rglob("*") if p.is_file()]
    srcs = build_sources(entries)
    print([(s.path, s.fmt, s.layer, s.zone) for s in srcs])
    log = []
    res = [r for s in srcs for r in fetch(s, Path(tempfile.mkdtemp()), None, td)]
    layers, struct = readers.ingest(res, log.append)
    print("\n".join(log))
    print(struct[["layer", "rule", "message"]].to_string())

    issues = qa.run_all_qa(layers, doms, struct)
    print("\nQA AVANT:\n", issues[["layer", "id", "rule", "severity", "message"]].to_string(max_colwidth=70))
    layers_raw = {k: v.copy() for k, v in layers.items()}

    clean, rej = preprocess_all(layers, doms, PreOpts(), log.append)
    issues2 = qa.run_all_qa(clean, doms, None)
    clean = qa.apply_status(clean, issues2)
    print("\nQA APRÈS:\n", issues2[["layer", "id", "rule", "severity", "message"]].to_string(max_colwidth=70))
    print(clean["Gares"][["id_fichegare", "id_orig", "axe_ferroviaire", "mode_acces_gare", "qa_status", "qa_flags"]])
    summ = qa.summarize(clean, issues2)
    print(summ.to_string())

    clean, rej = quarantine(clean, rej)
    b = exporters.build_bundle(clean, rej, issues2, summ, qa.completeness(clean), log, ["GeoPackage", "Shapefile", "KML"])
    out = td / "bundle.zip"
    out.write_bytes(b)
    import zipfile
    names = zipfile.ZipFile(out).namelist()
    print("\nBUNDLE:", names)
    # relecture des exports
    z = zipfile.ZipFile(out)
    z.extractall(td / "x")
    gp = next((td / "x").glob("*.gpkg"))
    import pyogrio
    print(pyogrio.list_layers(gp))
    print(gpd.read_file(next((td / "x" / "shp" / "Gares").glob("*.shp"))).columns.tolist())
    k = readers.read_kml(td / "x" / "kml" / "Gares.kml")
    print(k.shape, k.columns.tolist()[:6])
    shutil.rmtree(td)
    print("OK")


if __name__ == "__main__":
    main()
