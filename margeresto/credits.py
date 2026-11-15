"""Tarifs négociés : détection des écarts de facturation et demandes d'avoir.

Un prix facturé au-dessus du tarif convenu n'est pas une hausse à absorber :
c'est une erreur à se faire rembourser.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .db import get_or_create_supplier, transaction

TOLERANCE = 0.005  # 0,5 % : on ignore les écarts d'arrondi


def set_negotiated(conn, ingredient_id: int, supplier_name: str, price: float, valid_from: str) -> None:
    if price <= 0:
        raise ValueError("Le tarif doit être positif.")
    with transaction(conn):
        sid = get_or_create_supplier(conn, supplier_name)
        conn.execute("INSERT INTO negotiated_prices(ingredient_id, supplier_id, price, valid_from) "
                     "VALUES (?, ?, ?, ?)", (ingredient_id, sid, price, valid_from))


def negotiated_list(conn, ingredient_id: int) -> list[dict]:
    """Tarif en vigueur aujourd'hui pour chaque fournisseur de cet ingrédient."""
    rows = conn.execute(
        "SELECT s.name AS supplier, n.price, n.valid_from FROM negotiated_prices n "
        "JOIN suppliers s ON s.id = n.supplier_id WHERE n.ingredient_id = ? "
        "ORDER BY n.valid_from, n.id", (ingredient_id,)).fetchall()
    latest = {}
    for r in rows:
        latest[r["supplier"]] = dict(r)
    return list(latest.values())


@dataclass
class Discrepancy:
    line_id: int
    invoice_id: int
    supplier: str
    invoice_number: str
    invoice_date: str
    label: str
    ingredient: str
    base_unit: str
    quantity_base: float     # quantité facturée en unité de base
    paid: float              # € HT par unité de base
    agreed: float
    claimed: bool

    @property
    def amount(self) -> float:
        return (self.paid - self.agreed) * self.quantity_base


def billing_errors(conn, days: int | None = 90, include_claimed: bool = False) -> list[Discrepancy]:
    sql = """
        SELECT l.id AS line_id, i.id AS invoice_id, s.name AS supplier, i.invoice_number, i.invoice_date,
               l.label, g.name AS ingredient, g.base_unit,
               l.quantity * l.base_qty_per_unit AS qbase,
               l.unit_price_ht / l.base_qty_per_unit AS paid,
               (SELECT n.price FROM negotiated_prices n
                 WHERE n.ingredient_id = l.ingredient_id AND n.supplier_id = i.supplier_id
                   AND n.valid_from <= i.invoice_date
                 ORDER BY n.valid_from DESC, n.id DESC LIMIT 1) AS agreed,
               c.invoice_line_id IS NOT NULL AS claimed
        FROM invoice_lines l
        JOIN invoices i ON i.id = l.invoice_id
        JOIN suppliers s ON s.id = i.supplier_id
        JOIN ingredients g ON g.id = l.ingredient_id
        LEFT JOIN credit_claims c ON c.invoice_line_id = l.id
        WHERE l.base_qty_per_unit > 0
    """
    params: list = []
    if days is not None:
        last = conn.execute("SELECT MAX(invoice_date) FROM invoices").fetchone()[0]
        if last:
            sql += " AND i.invoice_date >= ?"
            params.append((date.fromisoformat(last) - timedelta(days=days)).isoformat())
    out = []
    for r in conn.execute(sql + " ORDER BY i.invoice_date DESC, l.id", params):
        if r["agreed"] is None or r["paid"] <= r["agreed"] * (1 + TOLERANCE):
            continue
        if r["claimed"] and not include_claimed:
            continue
        out.append(Discrepancy(r["line_id"], r["invoice_id"], r["supplier"], r["invoice_number"],
                               r["invoice_date"], r["label"], r["ingredient"], r["base_unit"],
                               r["qbase"], r["paid"], r["agreed"], bool(r["claimed"])))
    return out


def group_by_invoice(errors: list[Discrepancy]) -> list[list[Discrepancy]]:
    groups: dict[int, list[Discrepancy]] = {}
    for e in errors:
        groups.setdefault(e.invoice_id, []).append(e)
    return list(groups.values())


def _fr(x: float) -> str:
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def claim_email(items: list[Discrepancy], restaurant: str = "") -> tuple[str, str]:
    """Objet et corps d'un e-mail de demande d'avoir pour une facture."""
    first = items[0]
    d = date.fromisoformat(first.invoice_date).strftime("%d/%m/%Y")
    total = sum(e.amount for e in items)
    subject = f"Demande d'avoir - facture n° {first.invoice_number} du {d}"
    lines = [f"- {e.label} : facturé {_fr(e.paid)} € HT/{e.base_unit} au lieu de {_fr(e.agreed)} € "
             f"(tarif convenu), sur {e.quantity_base:g} {e.base_unit}, soit {_fr(e.amount)} € HT"
             for e in items]
    body = "\n".join([
        "Bonjour,",
        "",
        f"Sur votre facture n° {first.invoice_number} du {d}, les prix suivants ne correspondent pas "
        "à nos tarifs convenus :",
        "",
        *lines,
        "",
        f"Merci de nous adresser un avoir de {_fr(total)} € HT.",
        "",
        "Cordialement,",
        restaurant,
    ]).rstrip() + "\n"
    return subject, body


def mark_claimed(conn, items: list[Discrepancy]) -> None:
    with transaction(conn):
        for e in items:
            conn.execute("INSERT OR IGNORE INTO credit_claims(invoice_line_id, amount) VALUES (?, ?)",
                         (e.line_id, e.amount))
