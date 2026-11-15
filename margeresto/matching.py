"""Rapprochement des libellés fournisseurs avec les ingrédients, et lecture des conditionnements."""
from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz, process


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def label_key(label: str) -> str:
    """Clé stable d'un libellé : majuscules, sans accents ni ponctuation superflue."""
    s = strip_accents(label).upper()
    s = re.sub(r"[^A-Z0-9,.X ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# Conversion vers l'unité de base : (facteur, unité de base)
_UNITS = {
    "KG": (1.0, "kg"), "G": (0.001, "kg"), "GR": (0.001, "kg"),
    "L": (1.0, "L"), "LT": (1.0, "L"), "CL": (0.01, "L"), "ML": (0.001, "L"),
}
_NUM = r"(\d+(?:[.,]\d+)?)"
_PACK_RE = re.compile(rf"(?:{_NUM}\s*X\s*)?{_NUM}\s*(KG|GR|G|LT|L|CL|ML)\b")


def parse_pack(label: str) -> tuple[float, str] | None:
    """Devine la quantité de base contenue dans une unité facturée, d'après le libellé.

    'BEURRE DOUX 82% 10X250G'  -> (2.5, 'kg')
    'CREME LIQUIDE 35% 1L'     -> (1.0, 'L')
    'VIN ROUGE 75CL'           -> (0.75, 'L')
    Renvoie None si rien de fiable n'est trouvé (l'utilisateur complète).
    """
    s = label_key(label)
    matches = list(_PACK_RE.finditer(s))
    if not matches:
        return None
    m = matches[-1]  # le dernier conditionnement est le plus souvent le bon
    count = float(m.group(1).replace(",", ".")) if m.group(1) else 1.0
    size = float(m.group(2).replace(",", "."))
    factor, base = _UNITS[m.group(3)]
    return round(count * size * factor, 6), base


def base_qty_from_unit(unit: str, label: str, base_unit: str) -> float | None:
    """Quantité de base par unité facturée, à partir de l'unité de facturation puis du libellé."""
    u = label_key(unit or "")
    if u in ("KG", "KILO") and base_unit == "kg":
        return 1.0
    if u in ("L", "LT", "LITRE") and base_unit == "L":
        return 1.0
    if u in ("PCE", "PIECE", "U", "UN", "UNITE") and base_unit == "pièce":
        return 1.0
    pack = parse_pack(label)
    if pack and pack[1] == base_unit:
        return pack[0]
    return None


def suggest_ingredient(label: str, ingredients: list[dict], limit: int = 3) -> list[tuple[dict, float]]:
    """Propose les ingrédients les plus proches du libellé (score 0-100)."""
    if not ingredients:
        return []
    choices = {i["id"]: label_key(i["name"]) for i in ingredients}
    by_id = {i["id"]: i for i in ingredients}
    res = process.extract(label_key(label), choices, scorer=fuzz.token_set_ratio, limit=limit)
    return [(by_id[key], score) for _, score, key in res]
