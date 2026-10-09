"""Paramètres du monitoring. Les identifiants FTP viennent de l'environnement ou de st.secrets (jamais du code)."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent


def env(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    if not v:
        try:
            import streamlit as st
            v = st.secrets.get(key)
        except Exception:
            v = None
    return str(v) if v else default


def get_settings(base: Path | None = None) -> dict:
    b = Path(base) if base else BASE
    return {
        # FTP
        "ftp_path": env("PCDN_FTP_PATH", "/"), "ftp_recursive": True, "ftp_max_depth": 3,
        "extensions": ["shp", "kml", "zip", "dbf", "shx", "prj", "cpg"],
        # emprise plausible du corridor (lon/lat WGS84) : Douala -> N'Djamena
        "bbox": {"xmin": 8.0, "ymin": 2.0, "xmax": 17.5, "ymax": 14.5},
        "start_date": None,                      # ex. pd.Timestamp("2026-10-01")
        "date_cols": ["created", "creation_date", "date_jour", "date_collecte", "date", "change"],
        "agent_cols": ["enumerator", "agent", "agent_nom", "collecteur", "enqueteur", "username", "user"],
        "min_match_cols": 3,
        "id_convention": True,                   # ID attendu : ZONE<n> + 2 premières lettres de la couche + <n> (ex. ZONE4IN1)
        "id_pattern": None,                      # ex. r"^ZONE\d+[A-Z]{2}\d+$" si une convention d'ID est imposée
        "min_line_length_m": 5, "dup_point_tolerance_m": 2,
        # dossiers
        "config_dir": b / "config",
        "dir_ftp": b / "local" / "input" / "ftp", "dir_work": b / "local" / "work",
        "dir_output": b / "local" / "output", "dir_logs": b / "local" / "logs",
    }


def load_schema(config_dir: Path) -> dict:
    def rd(f):
        return pd.read_csv(Path(config_dir) / f, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8-sig")
    layers, vars_, dom = rd("pcdn_layers.csv"), rd("pcdn_schema.csv"), rd("pcdn_domains.csv")
    vars_["ordre"] = vars_["ordre"].astype(int)
    vars_["obligatoire"] = vars_["obligatoire"].str.upper().eq("TRUE")
    dom["ordre"] = dom["ordre"].astype(int)
    return {"layers": layers, "vars": vars_, "domains": dom}
