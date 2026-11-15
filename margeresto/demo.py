"""Données de démonstration : une brasserie, 5 mois de factures, 5 plats."""
from __future__ import annotations

from .db import add_quote, get_or_create_ingredient, save_invoice

MONTHS = ["2026-07-03", "2026-08-04", "2026-09-02", "2026-10-02", "2026-11-04"]

# ingrédient, unité de base, fournisseur, libellé facture, unité facturée,
# qté de base par unité facturée, qté achetée, prix HT par unité de base de juillet à novembre
CATALOG = [
    ("Bœuf haché 15 %", "kg", "Boucherie Martin", "STEAK HACHE 15% MG VRAC", "KG", 1, 160,
     [11.50, 11.60, 11.90, 12.60, 13.20]),
    ("Entrecôte", "kg", "Boucherie Martin", "ENTRECOTE VBF PAD", "KG", 1, 130,
     [29.00, 29.00, 29.80, 30.50, 31.50]),
    ("Pain burger brioché", "pièce", "Metro", "PAIN BURGER BRIOCHE 80G X24", "COLIS", 24, 38,
     [0.42, 0.42, 0.42, 0.43, 0.43]),
    ("Cheddar tranches", "kg", "Metro", "CHEDDAR TRANCHES 1KG", "PCE", 1, 36,
     [12.00, 12.00, 12.20, 12.40, 12.60]),
    ("Huile de friture", "L", "Metro", "HUILE FRITURE COLZA 25L", "BIDON", 25, 4,
     [2.10, 2.10, 2.12, 2.10, 2.10]),
    ("Beurre doux", "kg", "Metro", "BEURRE DOUX 82% MG 10X250G", "COLIS", 2.5, 8,
     [8.90, 9.00, 9.40, 9.90, 10.40]),
    ("Chocolat noir 70 %", "kg", "Metro", "CHOCOLAT NOIR 70% PISTOLES 5KG", "SAC", 5, 5,
     [14.00, 14.00, 14.00, 14.10, 14.10]),
    ("Œufs", "pièce", "Metro", "OEUFS PLEIN AIR CAL M X180", "PLATEAU", 180, 9,
     [0.22, 0.23, 0.24, 0.26, 0.27]),
    ("Sucre", "kg", "Metro", "SUCRE SEMOULE 1KG", "PCE", 1, 10,
     [1.10, 1.10, 1.10, 1.10, 1.10]),
    ("Pommes de terre", "kg", "Pomona", "POMME DE TERRE BINTJE SAC 25KG", "SAC", 25, 26,
     [0.85, 0.85, 0.88, 0.92, 0.95]),
    ("Crème liquide 35 %", "L", "Pomona", "CREME LIQUIDE 35% UHT 6X1L", "COLIS", 6, 8,
     [4.20, 4.25, 4.40, 4.55, 4.70]),
    ("Saumon (pavé)", "kg", "Pomona", "PAVE SAUMON ATL SANS PEAU 140/160G", "KG", 1, 65,
     [24.00, 24.00, 24.30, 24.60, 25.00]),
    ("Mesclun", "kg", "Pomona", "MESCLUN CAT1 1KG", "PCE", 1, 30,
     [9.50, 9.40, 9.20, 9.10, 9.00]),
    ("Tomate grappe", "kg", "Pomona", "TOMATE GRAPPE CAT1 5KG", "COLIS", 5, 8,
     [2.80, 2.70, 2.90, 3.20, 3.40]),
]

# Achats ponctuels chez un autre fournisseur, pour alimenter le comparateur :
# date, fournisseur, ingrédient, libellé, unité, qté de base par unité, qté, prix HT par unité de base
ALT_PURCHASES = [
    ("2026-09-18", "Pomona", "Beurre doux", "BEURRE PLAQUE 82% MG 1KG", "PCE", 1, 6, 9.45),
    ("2026-10-20", "Pomona", "Beurre doux", "BEURRE PLAQUE 82% MG 1KG", "PCE", 1, 6, 9.70),
    ("2026-10-20", "Pomona", "Œufs", "OEUFS PLEIN AIR CAL M X90", "PLATEAU", 90, 4, 0.25),
    ("2026-10-14", "Metro", "Crème liquide 35 %", "CREME LIQUIDE 35% 1L", "PCE", 1, 6, 4.85),
    ("2026-10-14", "Metro", "Bœuf haché 15 %", "VIANDE HACHEE 15% MG 5KG", "COLIS", 5, 4, 12.30),
]

# Devis / catalogues reçus : date, fournisseur, ingrédient, prix HT par unité de base, note
QUOTES = [
    ("2026-11-02", "Marée de Rungis", "Saumon (pavé)", 23.40, "Devis par e-mail, franco 300 €"),
    ("2026-08-10", "Metro", "Entrecôte", 30.20, "Prix catalogue"),
]

# Tarifs négociés : fournisseur, ingrédient, prix HT par unité de base, valable à partir du
NEGOTIATED = [
    ("Boucherie Martin", "Bœuf haché 15 %", 12.60, "2026-10-01"),   # facturé 13,20 € le 4/11 : écart
    ("Metro", "Beurre doux", 10.00, "2026-10-01"),                  # facturé 10,40 € le 4/11 : écart
    ("Pomona", "Pommes de terre", 0.95, "2026-10-01"),             # respecté
]

# plat, prix TTC, TVA, volume mensuel, [(ingrédient, quantité nette, rendement)]
DISHES = [
    ("Burger maison", 18.50, 0.10, 900, [
        ("Bœuf haché 15 %", 0.18, 1.0), ("Pain burger brioché", 1, 1.0), ("Cheddar tranches", 0.04, 1.0),
        ("Pommes de terre", 0.25, 0.8), ("Huile de friture", 0.03, 1.0), ("Mesclun", 0.02, 1.0),
        ("Tomate grappe", 0.04, 0.9)]),
    ("Entrecôte frites", 28.00, 0.10, 400, [
        ("Entrecôte", 0.30, 0.9), ("Pommes de terre", 0.25, 0.8), ("Huile de friture", 0.03, 1.0),
        ("Beurre doux", 0.02, 1.0)]),
    ("Pavé de saumon, crème citronnée", 24.00, 0.10, 350, [
        ("Saumon (pavé)", 0.18, 1.0), ("Crème liquide 35 %", 0.06, 1.0), ("Beurre doux", 0.02, 1.0),
        ("Mesclun", 0.03, 1.0)]),
    ("Omelette, salade et frites", 12.50, 0.10, 200, [
        ("Œufs", 3, 1.0), ("Beurre doux", 0.015, 1.0), ("Mesclun", 0.03, 1.0),
        ("Pommes de terre", 0.20, 0.8), ("Huile de friture", 0.02, 1.0)]),
    ("Mousse au chocolat", 8.50, 0.10, 450, [
        ("Chocolat noir 70 %", 0.05, 1.0), ("Œufs", 2, 1.0), ("Sucre", 0.015, 1.0),
        ("Crème liquide 35 %", 0.03, 1.0)]),
]


def load_demo(conn) -> None:
    """Remplit une base vide avec la brasserie de démonstration."""
    if conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]:
        raise RuntimeError("La base contient déjà des factures : vide-la avant de charger la démo.")
    ing_ids = {}
    for name, base, *_ in CATALOG:
        ing_ids[name] = get_or_create_ingredient(conn, name, base)
    conn.commit()

    suppliers = sorted({c[2] for c in CATALOG})
    for m, day in enumerate(MONTHS):
        for k, sup in enumerate(suppliers):
            lines = []
            for name, _base, s, label, unit, bq, qty, prices in CATALOG:
                if s != sup:
                    continue
                pu = round(prices[m] * bq, 2)
                lines.append({"label": label, "quantity": qty, "unit": unit, "unit_price_ht": pu,
                              "line_total_ht": round(qty * pu, 2), "ingredient_id": ing_ids[name],
                              "base_qty_per_unit": bq})
            if sup == "Metro":  # une ligne non-produit, ignorée dans les calculs
                lines.append({"label": "FRAIS DE LIVRAISON", "quantity": 1, "unit": "",
                              "unit_price_ht": 15.0, "line_total_ht": 15.0,
                              "ingredient_id": None, "base_qty_per_unit": None})
            total = round(sum(l["line_total_ht"] for l in lines), 2)
            save_invoice(conn, sup, f"F{day[:7].replace('-', '')}-{k + 1:02d}", day, lines, total,
                         source_file="démo")

    groups: dict[tuple[str, str], list[dict]] = {}
    for day, sup, ing, label, unit, bq, qty, price in ALT_PURCHASES:
        pu = round(price * bq, 2)
        groups.setdefault((day, sup), []).append({
            "label": label, "quantity": qty, "unit": unit, "unit_price_ht": pu,
            "line_total_ht": round(qty * pu, 2), "ingredient_id": ing_ids[ing], "base_qty_per_unit": bq})
    for k, ((day, sup), lines) in enumerate(sorted(groups.items())):
        save_invoice(conn, sup, f"P{day.replace('-', '')}-{k + 1:02d}", day, lines,
                     round(sum(l["line_total_ht"] for l in lines), 2), source_file="démo")
    for day, sup, ing, price, note in QUOTES:
        add_quote(conn, ing_ids[ing], sup, price, day, note)
    from .credits import set_negotiated
    for sup, ing, price, since in NEGOTIATED:
        set_negotiated(conn, ing_ids[ing], sup, price, since)

    for name, ttc, vat, vol, items in DISHES:
        did = conn.execute(
            "INSERT INTO dishes(name, price_ttc, vat_rate, monthly_volume) VALUES (?, ?, ?, ?)",
            (name, ttc, vat, vol)).lastrowid
        for ing, q, y in items:
            conn.execute("INSERT INTO dish_items(dish_id, ingredient_id, quantity, yield_pct) "
                         "VALUES (?, ?, ?, ?)", (did, ing_ids[ing], q, y))
    conn.commit()

    # Une hausse déjà traitée, pour montrer le compteur de marge récupérée :
    # les œufs ont augmenté chez Metro, le restaurant est passé chez Pomona il y a 9 jours.
    from . import actions
    from datetime import date, timedelta
    eggs = actions.alert_for(conn, ing_ids["Œufs"])
    option = actions.supplier_option(conn, eggs) if eggs else None
    if eggs and option:
        actions.apply_supplier(conn, eggs, option, (date.today() - timedelta(days=9)).isoformat())


def reset(conn) -> None:
    for t in ("credit_claims", "alert_actions", "negotiated_prices", "dish_items", "dishes",
              "product_mappings", "supplier_quotes", "invoice_lines", "invoices", "suppliers", "ingredients"):
        conn.execute(f"DELETE FROM {t}")
    conn.commit()
