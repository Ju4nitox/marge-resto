"""Alertes à traiter, plans d'action chiffrés et marge récupérée.

Une hausse devient une alerte quand le dernier prix payé dépasse de plus du seuil
le prix de base :
- la moyenne des 90 jours précédents si l'ingrédient n'a jamais été traité ;
- sinon le prix accepté lors du dernier traitement (pour ne pas réalerter
  sur une hausse déjà traitée).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import pricing, suppliers
from .db import get_setting, transaction

KINDS = {
    "prix_carte": "Prix de la carte ajustés",
    "fournisseur": "Changement de fournisseur",
    "grammage": "Grammage réduit",
    "absorbee": "Hausse absorbée",
}


@dataclass
class OpenAlert:
    ingredient_id: int
    name: str
    base_unit: str
    supplier: str
    price_before: float
    price_after: float
    price_date: str
    dishes: list = field(default_factory=list)   # pricing.DishImpact

    @property
    def change(self) -> float:
        return self.price_after / self.price_before - 1

    @property
    def monthly_impact(self) -> float:
        return sum(d.monthly_impact for d in self.dishes)


def last_action(conn, ingredient_id: int):
    return conn.execute(
        "SELECT * FROM alert_actions WHERE ingredient_id = ? ORDER BY price_date DESC, id DESC LIMIT 1",
        (ingredient_id,)).fetchone()


def alert_for(conn, ingredient_id: int, threshold: float | None = None) -> OpenAlert | None:
    if threshold is None:
        threshold = get_setting(conn, "alert_threshold")
    st = pricing.price_status(conn, ingredient_id)
    if st.current is None:
        return None
    la = last_action(conn, ingredient_id)
    if la is not None:
        if st.current_date <= la["price_date"]:
            return None                      # ce prix a déjà été traité
        base = la["price_after"]
    else:
        base = st.reference
    if not base or st.current <= base:
        return None
    imp = pricing.ingredient_impact(conn, ingredient_id, price_before=base)
    alert = OpenAlert(ingredient_id, st.name, st.base_unit, st.supplier, base, st.current,
                      st.current_date, imp["dishes"])
    # une hausse compte si elle est forte en % OU chère en euros
    if alert.change < threshold and -alert.monthly_impact < get_setting(conn, "alert_min_monthly"):
        return None
    return alert


def open_alerts(conn) -> list[OpenAlert]:
    ids = [r["id"] for r in conn.execute("SELECT id FROM ingredients")]
    alerts = [a for a in (alert_for(conn, i) for i in ids) if a is not None]
    return sorted(alerts, key=lambda a: (a.monthly_impact, -a.change))


# --- Options chiffrées ---------------------------------------------------------

def round_up_price(x: float, step: float = 0.10) -> float:
    """Arrondi au 10 centimes supérieur (prix de carte lisible)."""
    return math.ceil(round(x / step, 6)) * step


MIN_REPRICE_TTC = 0.05  # en dessous de 5 centimes par assiette, on ne touche pas au prix


def suggested_prices(alert: OpenAlert) -> dict[int, float]:
    """Prix TTC conseillé par plat : garde la même marge en euros, arrondi au 0,10 € supérieur.
    Les plats où la hausse pèse moins de 5 centimes par assiette ne sont pas repricés."""
    return {d.dish_id: round(round_up_price(d.price_ttc_to_keep_margin_eur), 2)
            for d in alert.dishes if d.extra_per_portion * (1 + d.vat_rate) >= MIN_REPRICE_TTC}


def reprice_recovery(alert: OpenAlert, new_ttc: dict[int, float]) -> float:
    out = 0.0
    for d in alert.dishes:
        if d.dish_id in new_ttc:
            out += (new_ttc[d.dish_id] - d.price_ttc) / (1 + d.vat_rate) * d.monthly_volume
    return out


def grammage_recovery(alert: OpenAlert, new_net_qty: dict[int, float]) -> float:
    out = 0.0
    for d in alert.dishes:
        if d.dish_id in new_net_qty:
            saved_gross = (d.net_qty - new_net_qty[d.dish_id]) / (d.yield_pct or 1.0)
            out += saved_gross * alert.price_after * d.monthly_volume
    return out


def supplier_option(conn, alert: OpenAlert):
    c = suppliers.compare_ingredient(conn, alert.ingredient_id)
    if c and c.monthly_saving > 0.5:
        return c
    return None


# --- Appliquer une option --------------------------------------------------------

def _record(conn, alert: OpenAlert, kind: str, recovered: float, detail: str,
            handled_on: str | None = None) -> float:
    """Enregistre le traitement et renvoie la marge récupérée retenue."""
    # on compte comme « récupérée » au plus la marge que la hausse a fait perdre
    recovered = min(max(recovered, 0.0), -alert.monthly_impact)
    conn.execute(
        "INSERT INTO alert_actions(ingredient_id, price_date, price_before, price_after, monthly_impact, "
        "kind, recovered_monthly, detail, handled_on) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alert.ingredient_id, alert.price_date, alert.price_before, alert.price_after, alert.monthly_impact,
         kind, recovered, detail, handled_on or date.today().isoformat()))
    return recovered


def apply_reprice(conn, alert: OpenAlert, new_ttc: dict[int, float], handled_on: str | None = None) -> float:
    rec = reprice_recovery(alert, new_ttc)
    names = {d.dish_id: d for d in alert.dishes}
    detail = "; ".join(f"{names[i].dish} : {names[i].price_ttc:.2f} → {p:.2f} €".replace(".", ",")
                       for i, p in new_ttc.items() if i in names)
    with transaction(conn):
        for dish_id, price in new_ttc.items():
            conn.execute("UPDATE dishes SET price_ttc = ? WHERE id = ?", (price, dish_id))
        return _record(conn, alert, "prix_carte", rec, detail, handled_on)


def apply_grammage(conn, alert: OpenAlert, new_net_qty: dict[int, float], handled_on: str | None = None) -> float:
    rec = grammage_recovery(alert, new_net_qty)
    names = {d.dish_id: d.dish for d in alert.dishes}
    with transaction(conn):
        for dish_id, q in new_net_qty.items():
            conn.execute("UPDATE dish_items SET quantity = ? WHERE dish_id = ? AND ingredient_id = ?",
                         (q, dish_id, alert.ingredient_id))
        return _record(conn, alert, "grammage", rec,
                       "; ".join(f"{names.get(i, i)} : {q:g}" for i, q in new_net_qty.items()), handled_on)


def apply_supplier(conn, alert: OpenAlert, comparison, handled_on: str | None = None) -> float:
    rec = comparison.monthly_saving
    with transaction(conn):
        return _record(conn, alert, "fournisseur", rec,
                f"Passage chez {comparison.best.supplier} à {comparison.best.price:.2f} €".replace(".", ","),
                       handled_on)


def absorb(conn, alert: OpenAlert, handled_on: str | None = None) -> None:
    with transaction(conn):
        _record(conn, alert, "absorbee", 0.0, "", handled_on)


# --- Bilan --------------------------------------------------------------------------

@dataclass
class Recovery:
    detected: float      # marge menacée par les hausses (€/mois, positif)
    recovered: float     # marge récupérée par les actions (€/mois)
    open: float          # reste à traiter (€/mois)
    handled: list = field(default_factory=list)


def recovery_summary(conn, days: int = 90) -> Recovery:
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT a.*, i.name FROM alert_actions a JOIN ingredients i ON i.id = a.ingredient_id "
        "WHERE a.handled_on >= ? ORDER BY a.handled_on DESC, a.id DESC", (since,)).fetchall()
    open_ = sum(-a.monthly_impact for a in open_alerts(conn))
    handled_loss = sum(-r["monthly_impact"] for r in rows)
    return Recovery(detected=handled_loss + open_, recovered=sum(r["recovered_monthly"] for r in rows),
                    open=open_, handled=[dict(r) for r in rows])
