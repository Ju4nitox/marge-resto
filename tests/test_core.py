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
    # fenêtre de 90 jours avant le 4 nov. = à partir du 6 août : achats de sept. et oct.
    assert st.reference == pytest.approx((9.40 + 9.90) / 2)
    # fenêtre de 120 jours : août entre aussi
    st120 = pricing.price_status(conn, beurre, reference_days=120)
    assert st120.reference == pytest.approx((9.00 + 9.40 + 9.90) / 3)


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
