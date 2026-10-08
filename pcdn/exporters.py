"""Exports : GeoPackage, Shapefile (noms ≤ 10 car.), KML (écrit à la main), rapport QA, archive ZIP."""
from __future__ import annotations

import io
import re
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import geopandas as gpd
import pandas as pd
import pyogrio

from .config import LAYERS

STATUS_COLOR = {"OK": "ff50b45a", "WARNING": "ff1e9bf5", "ERROR": "ff3c3cdc"}  # KML aabbggrr
HIDDEN = {"_rid", "geometry"}


def export_cols(g: gpd.GeoDataFrame) -> list[str]:
    return [c for c in g.columns if c not in HIDDEN]


def _prep(g: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Types compatibles écriture OGR."""
    g = g[export_cols(g) + ["geometry"]].copy()
    for c in g.columns:
        if str(g[c].dtype) == "Int64":
            g[c] = g[c].astype("float64") if g[c].isna().any() else g[c].astype("int64")
        elif str(g[c].dtype) == "boolean":
            g[c] = g[c].map({True: "Oui", False: "Non"})
    return g


def shp_names(cols) -> dict[str, str]:
    used, m = set(), {}
    for c in cols:
        s = c[:10]
        i = 0
        while s.lower() in used:
            i += 1
            s = f"{c[:10 - len(str(i)) - 1]}_{i}"
        used.add(s.lower())
        m[c] = s
    return m


# ------------------------------------------------------------------ GeoPackage
def to_gpkg(layers: dict, issues: pd.DataFrame, summary: pd.DataFrame, rejected: dict) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "PCDN.gpkg"
        for name, g in layers.items():
            _prep(g).to_file(p, layer=name, driver="GPKG", engine="pyogrio")
        for name, g in rejected.items():
            _prep(g.drop(columns=[c for c in g.columns if c == "_rid"], errors="ignore")).to_file(
                p, layer=f"{name}__rejets", driver="GPKG", engine="pyogrio")
        pyogrio.write_dataframe(issues.assign(severity=issues["severity"].astype(str)).drop(columns=[]), p,
                                layer="QA_anomalies", driver="GPKG")
        pyogrio.write_dataframe(summary, p, layer="QA_synthese", driver="GPKG")
        return p.read_bytes()


# ------------------------------------------------------------------ Shapefile
def to_shp_zip(layers: dict) -> bytes:
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as td, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        maps = []
        for name, g in layers.items():
            if g.empty:
                continue
            g = _prep(g)
            m = shp_names([c for c in g.columns if c != "geometry"])
            maps += [(name, long, short) for long, short in m.items()]
            d = Path(td) / name
            d.mkdir()
            g.rename(columns=m).to_file(d / f"{name}.shp", driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
            for f in d.iterdir():
                z.write(f, f"{name}/{f.name}")
        z.writestr("correspondance_champs_shp.csv",
                   pd.DataFrame(maps, columns=["couche", "champ_complet", "champ_shp"]).to_csv(index=False, encoding="utf-8-sig"))
    return buf.getvalue()


# ------------------------------------------------------------------ KML
_XML_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _x(v) -> str:
    return escape(_XML_BAD.sub("", str(v)))


def _coords(geom) -> str:
    return " ".join(f"{x:.7f},{y:.7f},0" for x, y, *_ in geom.coords)


def kml_folder(g: gpd.GeoDataFrame, layer: str) -> str:
    spec = LAYERS[layer]
    cols = [c for c in export_cols(g)]
    out = [f"<Folder><name>{_x(layer)}</name>"]
    for _, r in g.iterrows():
        geom = r.geometry
        if geom is None or geom.is_empty:
            continue
        status = r.get("qa_status", "OK")
        label = r.get(spec.id_field) or ""
        data = "".join(f'<Data name="{_x(c)}"><value>{_x(r[c])}</value></Data>'
                       for c in cols if not pd.isna(r[c]) and str(r[c]) != "")
        parts = geom.geoms if hasattr(geom, "geoms") else [geom]
        gx = "".join(f"<Point><coordinates>{p.x:.7f},{p.y:.7f},0</coordinates></Point>" if p.geom_type == "Point"
                     else f"<LineString><tessellate>1</tessellate><coordinates>{_coords(p)}</coordinates></LineString>"
                     for p in parts)
        if len(parts) > 1:
            gx = f"<MultiGeometry>{gx}</MultiGeometry>"
        out.append(f"<Placemark><name>{_x(label)}</name><styleUrl>#{status}</styleUrl>"
                   f"<ExtendedData>{data}</ExtendedData>{gx}</Placemark>")
    out.append("</Folder>")
    return "".join(out)


def _kml_doc(title: str, body: str) -> str:
    st = "".join(
        f'<Style id="{k}"><IconStyle><color>{c}</color><scale>1.1</scale></IconStyle>'
        f'<LineStyle><color>{c}</color><width>3</width></LineStyle></Style>' for k, c in STATUS_COLOR.items())
    return (f'<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f"<name>{_x(title)}</name>{st}{body}</Document></kml>")


def to_kml_files(layers: dict) -> dict[str, str]:
    files = {f"{n}.kml": _kml_doc(n, kml_folder(g, n)) for n, g in layers.items() if not g.empty}
    files["PCDN_toutes_couches.kml"] = _kml_doc("PCDN", "".join(kml_folder(g, n) for n, g in layers.items() if not g.empty))
    return files


# ------------------------------------------------------------------ rapport QA
def qa_report_xlsx(issues, summary, completeness, log_lines) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        summary.to_excel(w, sheet_name="Synthèse", index=False)
        issues.assign(severity=issues["severity"].astype(str)).to_excel(w, sheet_name="Anomalies", index=False)
        completeness.to_excel(w, sheet_name="Complétude", index=False)
        pd.DataFrame({"journal": log_lines}).to_excel(w, sheet_name="Journal", index=False)
        for ws in w.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = min(60, max(10, max(len(str(c.value or "")) for c in col[:200]) + 2))
    return buf.getvalue()


# ------------------------------------------------------------------ archive
def build_bundle(layers, rejected, issues, summary, completeness, log_lines, formats) -> bytes:
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if "GeoPackage" in formats:
            z.writestr(f"PCDN_{stamp}.gpkg", to_gpkg(layers, issues, summary, rejected))
        if "Shapefile" in formats:
            with zipfile.ZipFile(io.BytesIO(to_shp_zip(layers))) as s:
                for n in s.namelist():
                    z.writestr(f"shp/{n}", s.read(n))
        if "KML" in formats:
            for n, x in to_kml_files(layers).items():
                z.writestr(f"kml/{n}", x)
        z.writestr("qa/rapport_QA.xlsx", qa_report_xlsx(issues, summary, completeness, log_lines))
        z.writestr("qa/anomalies.csv", issues.assign(severity=issues["severity"].astype(str)).to_csv(index=False, encoding="utf-8-sig"))
        z.writestr("qa/journal.txt", "\n".join(log_lines))
    return buf.getvalue()
