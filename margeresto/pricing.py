"""Historique des prix, alertes de hausse et marge par plat."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .db import get_setting


def _d(s: str) -> date:
    return date.fromisoformat(s)


def price_history(conn, ingredient_id: int) -> list[dict]:
    """Prix par unité de base (kg, L, pièce), du plus ancien au plus récent."""
    rows = conn.execute(
        """
        SELECT i.invoice_date AS date, s.name AS supplier, l.label,
               l.unit_price_ht / l.base_qty_per_unit AS price
        FROM invoice_lines l
        JOIN invoices i ON i.id = l.invoice_id
        JOIN suppliers s ON s.id = i.supplier_id
        WHERE l.ingredient_id = ? AND l.base_qty_per_unit > 0
        ORDER BY i.invoice_date, l.id
        """,
        (ingredient_id,),
    ).fetchall()
    return [dict(r) for r in rows]


@dataclass
class PriceStatus:
    ingredient_id: int
    name: str
    base_unit: str
    current: float | None          # dernier prix payé
    current_date: str | None
    reference: float | None        # moyenne des achats de la période de référence
    supplier: str | None

    @property
    def change(self) -> float | None:
        if self.current is None or not self.reference:
            return None
        return self.current / self.reference - 1


def price_status(conn, ingredient_id: int, reference_days: int | None = None) -> PriceStatus:
    """Prix actuel (dernière date d'achat, moyenne si plusieurs lignes ce jour-là)
    et prix de référence (moyenne des achats des `reference_days` jours précédents)."""
    ing = conn.execute("SELECT name, base_unit FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
    hist = price_history(conn, ingredient_id)
    if reference_days is None:
        reference_days = int(get_setting(conn, "reference_days"))
    if not hist:
        return PriceStatus(ingredient_id, ing["name"], ing["base_unit"], None, None, None, None)

    last_date = hist[-1]["date"]
    last = [h for h in hist if h["date"] == last_date]
    current = sum(h["price"] for h in last) / len(last)

    start = _d(last_date) - timedelta(days=reference_days)
    window = [h for h in hist if start <= _d(h["date"]) < _d(last_date)]
    reference = sum(h["price"] for h in window) / len(window) if window else None
    return PriceStatus(ingredient_id, ing["name"], ing["base_unit"], current, last_date,
                       reference, last[-1]["supplier"])


def all_price_status(conn) -> list[PriceStatus]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM ingredients ORDER BY name")]
    return [price_status(conn, i) for i in ids]


def price_alerts(conn, threshold: float | None = None) -> list[PriceStatus]:
    """Ingrédients dont le dernier prix dépasse la référence d'au moins `threshold`."""
    if threshold is None:
        threshold = get_setting(conn, "alert_threshold")
    out = [p for p in all_price_status(conn) if p.change is not None and p.change >= threshold]
    return sorted(out, key=lambda p: p.change, reverse=True)


@dataclass
class DishReport:
    dish_id: int
    name: str
    price_ttc: float
    price_ht: float
    monthly_volume: float
    cost_now: float
    cost_ref: float
    missing_prices: list[str]

    @property
    def food_cost_now(self) -> float:
        return self.cost_now / self.price_ht if self.price_ht else 0.0

    @property
    def food_cost_ref(self) -> float:
        return self.cost_ref / self.price_ht if self.price_ht else 0.0

    @property
    def margin_rate_now(self) -> float:
        return 1 - self.food_cost_now

    @property
    def margin_rate_ref(self) -> float:
        return 1 - self.food_cost_ref

    @property
    def margin_drift_pts(self) -> float:
        """Variation du taux de marge, en points (négatif = la marge se dégrade)."""
        return (self.margin_rate_now - self.margin_rate_ref) * 100

    @property
    def monthly_impact(self) -> float:
        """Euros de marge perdus (négatif) ou gagnés par mois, à volumes constants."""
        return (self.cost_ref - self.cost_now) * self.monthly_volume

    def price_ttc_to_keep_margin_eur(self, vat_rate: float) -> float:
        """Prix TTC qui garde la même marge en euros par assiette (on répercute juste la hausse)."""
        return (self.price_ht + self.cost_now - self.cost_ref) * (1 + vat_rate)

    def price_ttc_to_keep_margin(self, vat_rate: float) -> float:
        """Prix TTC qui rétablit le taux de marge (%) de référence."""
        if self.food_cost_ref <= 0:
            return self.price_ttc
        return self.cost_now / self.food_cost_ref * (1 + vat_rate)

    def price_ttc_for_target(self, target_food_cost: float, vat_rate: float) -> float:
        return self.cost_now / target_food_cost * (1 + vat_rate)


def dish_report(conn, dish_id: int, statuses: dict[int, PriceStatus] | None = None) -> DishReport:
    d = conn.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone()
    items = conn.execute(
        "SELECT di.*, i.name FROM dish_items di JOIN ingredients i ON i.id = di.ingredient_id "
        "WHERE di.dish_id = ?",
        (dish_id,),
    ).fetchall()
    cost_now = cost_ref = 0.0
    missing = []
    for it in items:
        st = statuses.get(it["ingredient_id"]) if statuses else price_status(conn, it["ingredient_id"])
        if st is None or st.current is None:
            missing.append(it["name"])
            continue
        gross_qty = it["quantity"] / (it["yield_pct"] or 1.0)
        cost_now += gross_qty * st.current
        # Sans historique suffisant, la référence = le prix actuel (pas de dérive mesurable).
        cost_ref += gross_qty * (st.reference if st.reference else st.current)
    price_ht = d["price_ttc"] / (1 + d["vat_rate"])
    return DishReport(d["id"], d["name"], d["price_ttc"], price_ht, d["monthly_volume"],
                      cost_now, cost_ref, missing)


def all_dish_reports(conn) -> list[DishReport]:
    statuses = {p.ingredient_id: p for p in all_price_status(conn)}
    ids = [r["id"] for r in conn.execute("SELECT id FROM dishes ORDER BY name")]
    return [dish_report(conn, i, statuses) for i in ids]


# --- Impact d'un ingrédient sur la marge globale --------------------------------

def monthly_margin(conn) -> float:
    """Marge brute mensuelle de la carte (prix HT − coût matière actuel) × portions vendues."""
    return sum((r.price_ht - r.cost_now) * r.monthly_volume for r in all_dish_reports(conn))


@dataclass
class DishImpact:
    dish: str
    price_ttc: float
    vat_rate: float
    price_ht: float
    gross_qty: float          # quantité brute d'ingrédient par portion (avant rendement)
    base_unit: str
    monthly_volume: float
    cost_before: float        # coût matière du plat avant la variation
    extra_per_portion: float  # surcoût par portion dû à la variation
    dish_id: int | None = None
    net_qty: float = 0.0      # quantité nette dans l'assiette (fiche technique)
    yield_pct: float = 1.0

    @property
    def cost_after(self) -> float:
        return self.cost_before + self.extra_per_portion

    @property
    def margin_rate_before(self) -> float:
        return 1 - self.cost_before / self.price_ht if self.price_ht else 0.0

    @property
    def margin_rate_after(self) -> float:
        return 1 - self.cost_after / self.price_ht if self.price_ht else 0.0

    @property
    def monthly_impact(self) -> float:
        """Marge perdue (négatif) ou gagnée par mois."""
        return -self.extra_per_portion * self.monthly_volume

    @property
    def price_ttc_to_keep_margin_eur(self) -> float:
        return (self.price_ht + self.extra_per_portion) * (1 + self.vat_rate)


def ingredient_impact(conn, ingredient_id: int, simulated_change: float | None = None,
                      price_before: float | None = None) -> dict:
    """Effet d'une variation de prix d'un ingrédient sur chaque plat qui l'utilise.

    simulated_change=None : variation constatée (prix de référence -> dernier prix).
    simulated_change=0.10 : hausse simulée de 10 % à partir du dernier prix.
    """
    st_ = price_status(conn, ingredient_id)
    if st_.current is None:
        return {"status": st_, "price_before": None, "price_after": None, "dishes": []}
    if simulated_change is None:
        before = price_before if price_before is not None else (st_.reference or st_.current)
        after = st_.current
    else:
        before = st_.current
        after = st_.current * (1 + simulated_change)

    statuses = {p.ingredient_id: p for p in all_price_status(conn)}
    rows = conn.execute(
        "SELECT d.id, d.name, d.price_ttc, d.vat_rate, d.monthly_volume, di.quantity, di.yield_pct "
        "FROM dish_items di JOIN dishes d ON d.id = di.dish_id WHERE di.ingredient_id = ? ORDER BY d.name",
        (ingredient_id,),
    ).fetchall()
    dishes = []
    for r in rows:
        rep = dish_report(conn, r["id"], statuses)
        gross = r["quantity"] / (r["yield_pct"] or 1.0)
        # coût "avant" = coût actuel du plat, avec cet ingrédient au prix "avant"
        cost_before = rep.cost_now - gross * (st_.current - before)
        dishes.append(DishImpact(r["name"], r["price_ttc"], r["vat_rate"], rep.price_ht, gross,
                                 st_.base_unit, r["monthly_volume"], cost_before,
                                 gross * (after - before), r["id"], r["quantity"], r["yield_pct"] or 1.0))
    return {"status": st_, "price_before": before, "price_after": after, "dishes": dishes}


def impact_ranking(conn) -> list[dict]:
    """Tous les ingrédients utilisés dans la carte, classés par marge perdue par mois
    du fait de leur variation de prix constatée."""
    out = []
    used = conn.execute("SELECT DISTINCT ingredient_id FROM dish_items").fetchall()
    for row in used:
        imp = ingredient_impact(conn, row["ingredient_id"])
        st_ = imp["status"]
        if st_.current is None:
            continue
        out.append({
            "ingredient_id": st_.ingredient_id, "name": st_.name, "supplier": st_.supplier,
            "change": st_.change, "dishes": len(imp["dishes"]),
            "monthly_impact": sum(d.monthly_impact for d in imp["dishes"]),
        })
    return sorted(out, key=lambda x: x["monthly_impact"])
