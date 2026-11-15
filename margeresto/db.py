"""Base SQLite : schéma et accès."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "marge.db"

BASE_UNITS = ("kg", "L", "pièce")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingredients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    base_unit TEXT NOT NULL CHECK (base_unit IN ('kg', 'L', 'pièce')),
    category TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    invoice_number TEXT NOT NULL,
    invoice_date TEXT NOT NULL,          -- AAAA-MM-JJ
    total_ht REAL,
    source_file TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (supplier_id, invoice_number)
);

-- Une ligne de facture. base_qty_per_unit = quantité d'unité de base (kg, L, pièce)
-- contenue dans UNE unité facturée (ex. colis 6 x 2,5 kg -> 15).
CREATE TABLE IF NOT EXISTS invoice_lines (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    reference TEXT DEFAULT '',
    quantity REAL NOT NULL,
    unit TEXT DEFAULT '',
    unit_price_ht REAL NOT NULL,
    line_total_ht REAL,
    ingredient_id INTEGER REFERENCES ingredients(id),
    base_qty_per_unit REAL
);

-- Mémorise "ce libellé chez ce fournisseur = cet ingrédient", pour ne valider qu'une fois.
CREATE TABLE IF NOT EXISTS product_mappings (
    id INTEGER PRIMARY KEY,
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    label_key TEXT NOT NULL,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    base_qty_per_unit REAL NOT NULL,
    UNIQUE (supplier_id, label_key)
);

CREATE TABLE IF NOT EXISTS dishes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    price_ttc REAL NOT NULL,
    vat_rate REAL NOT NULL DEFAULT 0.10,
    monthly_volume REAL NOT NULL DEFAULT 0   -- portions vendues / mois (optionnel)
);

-- quantity = quantité nette dans l'assiette, en unité de base de l'ingrédient.
-- yield_pct = rendement (ex. 0.8 si 20 % de perte au parage) ; coût = quantity / yield x prix.
CREATE TABLE IF NOT EXISTS dish_items (
    id INTEGER PRIMARY KEY,
    dish_id INTEGER NOT NULL REFERENCES dishes(id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients(id),
    quantity REAL NOT NULL,
    yield_pct REAL NOT NULL DEFAULT 1.0
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "alert_threshold": "0.05",     # alerte si +5 % vs prix de référence
    "reference_days": "90",        # fenêtre du prix de référence
    "target_food_cost": "0.30",    # ratio matière cible par plat
}


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init(conn)
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get_setting(conn, key: str) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return float(row["value"] if row else DEFAULT_SETTINGS[key])


def set_setting(conn, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


# --- Écritures simples -------------------------------------------------------

def get_or_create_supplier(conn, name: str) -> int:
    name = name.strip()
    row = conn.execute("SELECT id FROM suppliers WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute("INSERT INTO suppliers(name) VALUES (?)", (name,)).lastrowid


def get_or_create_ingredient(conn, name: str, base_unit: str, category: str = "") -> int:
    if base_unit not in BASE_UNITS:
        raise ValueError(f"Unité de base inconnue : {base_unit}")
    name = name.strip()
    row = conn.execute("SELECT id FROM ingredients WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return conn.execute(
        "INSERT INTO ingredients(name, base_unit, category) VALUES (?, ?, ?)",
        (name, base_unit, category),
    ).lastrowid


def invoice_exists(conn, supplier_name: str, invoice_number: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM invoices i JOIN suppliers s ON s.id = i.supplier_id "
        "WHERE s.name = ? AND i.invoice_number = ?",
        (supplier_name.strip(), invoice_number.strip()),
    ).fetchone()
    return row is not None


def save_invoice(conn, supplier_name: str, invoice_number: str, invoice_date: str,
                 lines: list[dict], total_ht: float | None = None,
                 source_file: str = "", remember_mappings: bool = True) -> int:
    """Enregistre une facture validée.

    Chaque ligne : label, reference, quantity, unit, unit_price_ht, line_total_ht,
    ingredient_id (ou None pour ignorer : frais de port, consigne...), base_qty_per_unit.
    """
    from .matching import label_key

    with transaction(conn):
        sid = get_or_create_supplier(conn, supplier_name)
        inv_id = conn.execute(
            "INSERT INTO invoices(supplier_id, invoice_number, invoice_date, total_ht, source_file) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, invoice_number.strip(), invoice_date, total_ht, source_file),
        ).lastrowid
        for ln in lines:
            ing = ln.get("ingredient_id")
            bq = ln.get("base_qty_per_unit")
            conn.execute(
                "INSERT INTO invoice_lines(invoice_id, label, reference, quantity, unit, "
                "unit_price_ht, line_total_ht, ingredient_id, base_qty_per_unit) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (inv_id, ln["label"], ln.get("reference") or "", float(ln["quantity"]),
                 ln.get("unit") or "", float(ln["unit_price_ht"]), ln.get("line_total_ht"),
                 ing, bq),
            )
            if remember_mappings and ing and bq:
                conn.execute(
                    "INSERT INTO product_mappings(supplier_id, label_key, ingredient_id, base_qty_per_unit) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(supplier_id, label_key) DO UPDATE SET "
                    "ingredient_id = excluded.ingredient_id, base_qty_per_unit = excluded.base_qty_per_unit",
                    (sid, label_key(ln["label"]), ing, bq),
                )
    return inv_id


def known_mapping(conn, supplier_name: str, label: str):
    from .matching import label_key

    return conn.execute(
        "SELECT m.ingredient_id, m.base_qty_per_unit FROM product_mappings m "
        "JOIN suppliers s ON s.id = m.supplier_id WHERE s.name = ? AND m.label_key = ?",
        (supplier_name.strip(), label_key(label)),
    ).fetchone()
