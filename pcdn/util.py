"""Utilitaires : normalisation de noms/valeurs, nettoyage des vides, journal."""
from __future__ import annotations

import re
import unicodedata
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore", message=r".*Measured \(M\) geometry.*")
warnings.filterwarnings("ignore", message=r".*Geometry is in a geographic CRS.*")

_SPECIAL = {"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "’": "'", "–": "-", "—": "-"}


def ascii_fold(s: str) -> str:
    for a, b in _SPECIAL.items():
        s = s.replace(a, b)
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def is_null(v) -> bool:
    if v is None or v is pd.NA or v is pd.NaT:
        return True
    return isinstance(v, float) and np.isnan(v)


def norm_key(x) -> str:
    """Nom de colonne normalisé : minuscules, sans accents, [a-z0-9_]."""
    if is_null(x):
        return ""
    s = ascii_fold(str(x)).lower()
    return re.sub(r"^_+|_+$", "", re.sub(r"[^a-z0-9]+", "_", s))


def norm_val(x) -> str:
    """Valeur de liste normalisée pour comparaison (accents, casse, tirets, < >)."""
    if is_null(x):
        return ""
    s = str(x).replace("<", " lt ").replace(">", " gt ")
    s = ascii_fold(s).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


NA_TOKENS = {"", "n/a", "na", "null", "none", "nan", "-- selectionnez une valeur --", "-- sélectionnez une valeur --"}


def clean_na(s) -> pd.Series:
    """Texte nettoyé (espaces), vides/jetons NA -> None. Toujours dtype=object (pandas 3 : None reste None)."""
    ser = s if isinstance(s, pd.Series) else pd.Series(list(s))
    out = []
    for v in ser.tolist():
        if is_null(v):
            out.append(None)
            continue
        t = re.sub(r"\s+", " ", str(v)).strip()
        if len(t) >= 2 and t[0] == t[-1] == '"':          # valeurs Mapit entre guillemets ("Bitume")
            t = t[1:-1].strip()
        out.append(None if t.lower() in NA_TOKENS else t)
    return pd.Series(out, index=ser.index, dtype="object")


def coalesce(a: pd.Series, b: pd.Series) -> pd.Series:
    return pd.Series([x if x is not None else y for x, y in zip(a.tolist(), b.tolist())], index=a.index, dtype="object")


class Logger:
    """Journal en mémoire (+ fichier optionnel)."""

    def __init__(self, path=None, echo=False):
        self.lines: list[str] = []
        self.path, self.echo = path, echo
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, *msg, level="INFO"):
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level:<5} {''.join(str(m) for m in msg)}"
        self.lines.append(line)
        if self.echo:
            print(line)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
