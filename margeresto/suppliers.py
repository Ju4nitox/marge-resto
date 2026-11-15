"""Comparateur de fournisseurs : meilleure offre par produit et économie possible."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .db import get_setting

STALE_DAYS = 60  # au-delà, le prix d'un autre fournisseur est « à confirmer »


@dataclass
class Offer:
    supplier: str
    price: float          # € HT par unité de base
    date: str
    source: str           # "facture" ou "devis"


@dataclass
class Comparison:
    ingredient_id: int
    name: str
    base_unit: str
    current: Offer                      # dernier achat réel
    offers: list[Offer] = field(default_factory=list)   # dernière offre connue par fournisseur
    monthly_qty: float = 0.0            # quantité achetée par mois (unité de base)

    @property
    def best(self) -> Offer:
        """Meilleure offre récente : un prix de plus de STALE_DAYS jours ne compte pas
        dans l'économie (il est seulement signalé, voir stale_cheaper)."""
        fresh = [o for o in self.offers if not self.is_stale(o) or o.supplier == self.current.supplier]
        return min(fresh, key=lambda o: o.price)

    @property
    def stale_cheaper(self) -> list[Offer]:
        """Offres moins chères mais trop anciennes : à redemander au fournisseur."""
        return [o for o in self.offers if self.is_stale(o) and o.price < self.best.price]

    @property
    def has_alternative(self) -> bool:
        return len({o.supplier for o in self.offers}) > 1

    @property
    def gap(self) -> float:
        """Écart relatif du prix actuel au meilleur prix (0,07 = 7 % plus cher)."""
        return self.current.price / self.best.price - 1 if self.best.price else 0.0

    @property
    def monthly_saving(self) -> float:
        if self.best.supplier == self.current.supplier:
            return 0.0
        return (self.current.price - self.best.price) * self.monthly_qty

    def is_stale(self, offer: Offer) -> bool:
        return (date.fromisoformat(self.current.date) - date.fromisoformat(offer.date)).days > STALE_DAYS


def compare_ingredient(conn, ingredient_id: int, reference_days: int | None = None) -> Comparison | None:
    ing = conn.execute("SELECT name, base_unit FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
    purchases = conn.execute(
        """
        SELECT s.name AS supplier, i.invoice_date AS date,
               l.unit_price_ht / l.base_qty_per_unit AS price, l.quantity * l.base_qty_per_unit AS qty
        FROM invoice_lines l
        JOIN invoices i ON i.id = l.invoice_id
        JOIN suppliers s ON s.id = i.supplier_id
        WHERE l.ingredient_id = ? AND l.base_qty_per_unit > 0
        ORDER BY i.invoice_date, l.id
        """, (ingredient_id,)).fetchall()
    if not purchases:
        return None
    quotes = conn.execute(
        "SELECT s.name AS supplier, q.quote_date AS date, q.price FROM supplier_quotes q "
        "JOIN suppliers s ON s.id = q.supplier_id WHERE q.ingredient_id = ? ORDER BY q.quote_date, q.id",
        (ingredient_id,)).fetchall()

    last = purchases[-1]
    current = Offer(last["supplier"], last["price"], last["date"], "facture")

    latest: dict[str, Offer] = {}
    events = [Offer(p["supplier"], p["price"], p["date"], "facture") for p in purchases] + \
             [Offer(q["supplier"], q["price"], q["date"], "devis") for q in quotes]
    for o in sorted(events, key=lambda o: o.date):
        latest[o.supplier] = o            # garde l'offre la plus récente de chaque fournisseur
    latest[current.supplier] = current

    if reference_days is None:
        reference_days = int(get_setting(conn, "reference_days"))
    start = date.fromisoformat(last["date"]) - timedelta(days=reference_days)
    bought = sum(p["qty"] for p in purchases if date.fromisoformat(p["date"]) > start)
    monthly_qty = bought / (reference_days / 30)

    return Comparison(ingredient_id, ing["name"], ing["base_unit"], current,
                      sorted(latest.values(), key=lambda o: o.price), monthly_qty)


def compare_all(conn) -> list[Comparison]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM ingredients ORDER BY name")]
    out = [c for c in (compare_ingredient(conn, i) for i in ids) if c is not None]
    return sorted(out, key=lambda c: c.monthly_saving, reverse=True)
