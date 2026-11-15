import pytest

from margeresto import db, demo, pricing
from margeresto.extraction import clean
from margeresto.matching import base_qty_from_unit, label_key, parse_pack, suggest_ingredient


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    demo.load_demo(c)
    return c


@pytest.mark.parametrize("label,expected", [
    ("BEURRE DOUX 82% MG 10X250G", (2.5, "kg")),
    ("CREME LIQUIDE 35% UHT 6X1L", (6.0, "L")),
    ("VIN ROUGE 75CL", (0.75, "L")),
    ("Farine T55 sac 25 kg", (25.0, "kg")),
    ("HUILE 1,5L", (1.5, "L")),
    ("PERSIL BOTTE", None),
])
def test_parse_pack(label, expected):
    assert parse_pack(label) == expected


def test_base_qty_from_unit():
    assert base_qty_from_unit("KG", "ENTRECOTE", "kg") == 1.0
    assert base_qty_from_unit("COLIS", "CREME 6X1L", "L") == 6.0
    assert base_qty_from_unit("COLIS", "CREME 6X1L", "kg") is None  # unité incohérente


def test_label_key_stable():
    assert label_key("Crème liquide 35%  6x1L") == label_key("CREME LIQUIDE 35% 6X1L")


def test_suggest_ingredient():
    ings = [{"id": 1, "name": "Crème liquide 35 %"}, {"id": 2, "name": "Beurre doux"}]
    best, score = suggest_ingredient("CREME LIQUIDE 35% UHT 6X1L", ings, 1)[0]
    assert best["id"] == 1 and score >= 70


def test_price_status_reference_window(conn):
    beurre = conn.execute("SELECT id FROM ingredients WHERE name = 'Beurre doux'").fetchone()[0]
    st = pricing.price_status(conn, beurre, reference_days=90)
    assert st.current == pytest.approx(10.40)
    # fenêtre de 90 jours avant le 4 nov. = à partir du 6 août :
    # Metro 2/09 et 2/10, Pomona 18/09 et 20/10
    assert st.reference == pytest.approx((9.40 + 9.45 + 9.90 + 9.70) / 4)
    # fenêtre de 120 jours : l'achat Metro du 4 août entre aussi
    st120 = pricing.price_status(conn, beurre, reference_days=120)
    assert st120.reference == pytest.approx((9.00 + 9.40 + 9.45 + 9.90 + 9.70) / 5)


def test_alerts_sorted_and_thresholded(conn):
    alerts = pricing.price_alerts(conn, threshold=0.05)
    names = [a.name for a in alerts]
    assert "Beurre doux" in names and "Mesclun" not in names  # le mesclun baisse
    assert all(a.change >= 0.05 for a in alerts)
    assert [a.change for a in alerts] == sorted([a.change for a in alerts], reverse=True)


def test_dish_report_burger(conn):
    burger = conn.execute("SELECT id FROM dishes WHERE name = 'Burger maison'").fetchone()[0]
    r = pricing.dish_report(conn, burger)
    expected_now = 0.18 * 13.20 + 0.43 + 0.04 * 12.60 + 0.25 / 0.8 * 0.95 + 0.03 * 2.10 + 0.02 * 9.00 + 0.04 / 0.9 * 3.40
    assert r.cost_now == pytest.approx(expected_now)
    assert r.price_ht == pytest.approx(18.50 / 1.1)
    assert r.margin_drift_pts < 0 and r.monthly_impact < 0
    # répercuter la hausse en euros rend exactement la marge en euros d'avant
    new_ht = r.price_ttc_to_keep_margin_eur(0.10) / 1.1
    assert new_ht - r.cost_now == pytest.approx(r.price_ht - r.cost_ref)


def test_ignored_lines_not_priced(conn):
    n = conn.execute("SELECT COUNT(*) FROM invoice_lines WHERE ingredient_id IS NULL").fetchone()[0]
    assert n == 5  # une ligne de livraison Metro par mois


def test_mapping_remembered(conn):
    m = db.known_mapping(conn, "Pomona", "creme liquide 35% uht 6x1l")
    assert m is not None and m["base_qty_per_unit"] == 6


def test_duplicate_invoice_rejected(conn):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.save_invoice(conn, "Metro", "F202607-02", "2026-07-03", [])


def test_clean_flags_inconsistent_line():
    out = clean({"supplier": "X", "invoice_number": "1", "invoice_date": "2026-11-01",
                 "lines": [{"label": "A", "quantity": 2, "unit": "KG", "unit_price_ht": 5,
                            "line_total_ht": 14, "is_product": True}]})
    assert "≠" in out["lines"][0]["warning"]


def test_ingredient_impact_observed(conn):
    beurre = conn.execute("SELECT id FROM ingredients WHERE name = 'Beurre doux'").fetchone()[0]
    imp = pricing.ingredient_impact(conn, beurre)
    names = {d.dish for d in imp["dishes"]}
    assert names == {"Entrecôte frites", "Pavé de saumon, crème citronnée", "Omelette, salade et frites"}
    delta = 10.40 - (9.40 + 9.45 + 9.90 + 9.70) / 4
    entrecote = next(d for d in imp["dishes"] if d.dish == "Entrecôte frites")
    assert entrecote.extra_per_portion == pytest.approx(0.02 * delta)
    assert entrecote.monthly_impact == pytest.approx(-0.02 * delta * 400)
    assert entrecote.margin_rate_after < entrecote.margin_rate_before


def test_ingredient_impact_simulated(conn):
    boeuf = conn.execute("SELECT id FROM ingredients WHERE name = 'Bœuf haché 15 %'").fetchone()[0]
    imp = pricing.ingredient_impact(conn, boeuf, simulated_change=0.10)
    (burger,) = imp["dishes"]
    assert burger.extra_per_portion == pytest.approx(0.18 * 13.20 * 0.10)
    assert burger.monthly_impact == pytest.approx(-0.18 * 13.20 * 0.10 * 900)
    # coût "avant" en simulation = coût actuel du plat
    rep = pricing.dish_report(conn, conn.execute("SELECT id FROM dishes WHERE name='Burger maison'").fetchone()[0])
    assert burger.cost_before == pytest.approx(rep.cost_now)


def test_impact_ranking_sums_to_dish_drift(conn):
    ranking = pricing.impact_ranking(conn)
    total = sum(r["monthly_impact"] for r in ranking)
    assert total == pytest.approx(sum(r.monthly_impact for r in pricing.all_dish_reports(conn)))
    assert ranking[0]["monthly_impact"] <= ranking[-1]["monthly_impact"]


# --- Comparateur fournisseurs ---------------------------------------------------

from margeresto import suppliers


def _ing(conn, name):
    return conn.execute("SELECT id FROM ingredients WHERE name = ?", (name,)).fetchone()[0]


def test_compare_finds_cheaper_supplier_from_invoice(conn):
    c = suppliers.compare_ingredient(conn, _ing(conn, "Bœuf haché 15 %"))
    assert c.current.supplier == "Boucherie Martin" and c.current.price == pytest.approx(13.20)
    assert c.best.supplier == "Metro" and c.best.price == pytest.approx(12.30)
    # 3 livraisons de 160 kg + 20 kg chez Metro sur 90 jours -> 500 / 3 kg par mois
    assert c.monthly_qty == pytest.approx(500 / 3)
    assert c.monthly_saving == pytest.approx((13.20 - 12.30) * 500 / 3)
    assert not c.is_stale(c.best)


def test_compare_uses_recent_quotes(conn):
    c = suppliers.compare_ingredient(conn, _ing(conn, "Saumon (pavé)"))
    assert c.best.source == "devis" and c.best.supplier == "Marée de Rungis"
    assert c.monthly_saving > 0


def test_old_quote_not_counted_as_saving(conn):
    c = suppliers.compare_ingredient(conn, _ing(conn, "Entrecôte"))
    # devis Metro d'août à 30,20 € : moins cher, mais trop ancien pour être promis
    assert c.best.supplier == "Boucherie Martin" and c.monthly_saving == 0
    assert [o.supplier for o in c.stale_cheaper] == ["Metro"]


def test_compare_no_saving_when_already_cheapest(conn):
    c = suppliers.compare_ingredient(conn, _ing(conn, "Crème liquide 35 %"))
    assert c.has_alternative and c.best.supplier == c.current.supplier
    assert c.monthly_saving == 0


def test_add_quote_rejects_bad_price(conn):
    with pytest.raises(ValueError):
        db.add_quote(conn, _ing(conn, "Sucre"), "Promocash", 0, "2026-11-10")


def test_compare_all_sorted(conn):
    res = suppliers.compare_all(conn)
    savings = [c.monthly_saving for c in res]
    assert savings == sorted(savings, reverse=True) and savings[0] > 0
