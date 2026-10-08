from __future__ import annotations

import re
import unicodedata

import pandas as pd

NULLS = {"", "nan", "none", "null", "<null>", "nat", "<na>"}
BOOL_T = {"true", "vrai", "oui", "yes", "y", "o", "1", "t", "x"}
BOOL_F = {"false", "faux", "non", "no", "n", "0", "f"}


def is_null(v) -> bool:
    if v is None:
        return True
    try:
        if pd.isna(v):
            return True
    except (TypeError, ValueError):
        pass
    return str(v).strip().lower() in NULLS


def norm(s) -> str:
    """Clé de comparaison : sans accents/casse, tirets et apostrophes unifiés."""
    if s is None:
        return ""
    s = str(s).replace("’", "'").replace("‘", "'").replace("œ", "oe").replace("Œ", "oe").replace("×", "x")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"\s*[-–—−‐]\s*", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_text(v):
    if is_null(v):
        return None
    s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(v))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def split_multi(v, domain_norm: set | None = None) -> list[str]:
    """Découpe une valeur MULTISELECT (séparateurs ; | ou virgule)."""
    if is_null(v):
        return []
    s = str(v).strip()
    if domain_norm and norm(s) in domain_norm:
        return [s]
    toks = []
    for t in re.split(r"\s*[;|]\s*", s):
        if domain_norm and norm(t) not in domain_norm and "," in t:
            toks += [x for x in re.split(r"\s*,\s*", t)]
        else:
            toks.append(t)
    return [t.strip() for t in toks if t.strip()]


def parse_bool(s: pd.Series):
    """-> (série 'boolean', masque des valeurs non interprétables)."""
    def f(v):
        if isinstance(v, bool):
            return v
        if is_null(v):
            return pd.NA
        n = norm(v)
        if n in BOOL_T:
            return True
        if n in BOOL_F:
            return False
        return "?"
    r = s.map(f)
    bad = r.map(lambda x: isinstance(x, str))
    r = r.where(~bad, pd.NA)
    return r.astype("boolean"), bad


def parse_num(s: pd.Series):
    """-> (float64, masque invalides)."""
    raw = s.map(lambda v: None if is_null(v) else str(v).replace(",", ".").replace(" ", ""))
    num = pd.to_numeric(raw, errors="coerce")
    bad = raw.notna() & num.isna()
    return num.astype("float64"), bad
