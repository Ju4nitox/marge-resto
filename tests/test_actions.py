import io
from datetime import date

import pytest

from margeresto import actions, credits, db, demo, notify
from margeresto.extraction import read_invoice
from margeresto.facturx import FacturXError, parse_cii


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    demo.load_demo(c)
    return c


def _ing(conn, name):
    return conn.execute("SELECT id FROM ingredients WHERE name = ?", (name,)).fetchone()[0]


def _dish(conn, name):
    return conn.execute("SELECT id FROM dishes WHERE name = ?", (name,)).fetchone()[0]


# --- Alertes ---------------------------------------------------------------------

def test_alert_on_percent_or_euros(conn):
    names = [a.name for a in actions.open_alerts(conn)]
    # l'entrecôte ne monte que de 4,5 % (sous le seuil de 5 %) mais coûte 180 €/mois : alerte
    assert names[0] == "Entrecôte"
    # le mesclun baisse : pas d'alerte ; les œufs sont déjà traités dans la démo
    assert "Mesclun" not in names and "Œufs" not in names


def test_alert_sorted_by_cost(conn):
    impacts = [a.monthly_impact for a in actions.open_alerts(conn)]
    assert impacts == sorted(impacts)


def test_handled_alert_does_not_come_back_for_same_price(conn):
    a = actions.alert_for(conn, _ing(conn, "Tomate grappe"))
    actions.absorb(conn, a)
    assert actions.alert_for(conn, _ing(conn, "Tomate grappe")) is None


def test_new_hike_after_handling_reopens_from_accepted_price(conn):
    tom = _ing(conn, "Tomate grappe")
    actions.absorb(conn, actions.alert_for(conn, tom))      # accepte 3,40 €/kg
    db.save_invoice(conn, "Pomona", "F-NEW", "2026-11-20", [{
        "label": "TOMATE GRAPPE CAT1 5KG", "quantity": 8, "unit": "COLIS", "unit_price_ht": 19.0,
        "ingredient_id": tom, "base_qty_per_unit": 5}])        # 3,80 €/kg
    a = actions.alert_for(conn, tom)
    assert a is not None and a.price_before == pytest.approx(3.40) and a.price_after == pytest.approx(3.80)


def test_suggested_prices_round_up_and_skip_tiny(conn):
    a = actions.alert_for(conn, _ing(conn, "Bœuf haché 15 %"))
    sugg = actions.suggested_prices(a)
    burger = a.dishes[0]
    assert sugg[burger.dish_id] == pytest.approx(18.70)          # 18,68 € arrondi au 0,10 supérieur
    assert sugg[burger.dish_id] >= burger.price_ttc_to_keep_margin_eur
    pdt = actions.alert_for(conn, _ing(conn, "Pommes de terre"))
    assert actions.suggested_prices(pdt) == {}                   # moins de 5 centimes par assiette


def test_apply_reprice_updates_menu_and_caps_recovery(conn):
    a = actions.alert_for(conn, _ing(conn, "Bœuf haché 15 %"))
    burger = _dish(conn, "Burger maison")
    rec = actions.apply_reprice(conn, a, {burger: 18.70})
    assert conn.execute("SELECT price_ttc FROM dishes WHERE id = ?", (burger,)).fetchone()[0] == 18.70
    # (18,70 − 18,50) / 1,1 × 900 = 163,6 € > perte de 151 € : on compte au plus la perte
    stored = conn.execute("SELECT recovered_monthly FROM alert_actions WHERE kind = 'prix_carte'").fetchone()[0]
    assert 0.20 / 1.1 * 900 > -a.monthly_impact
    assert rec == stored == pytest.approx(-a.monthly_impact)
    assert actions.alert_for(conn, a.ingredient_id) is None


def test_apply_grammage(conn):
    a = actions.alert_for(conn, _ing(conn, "Entrecôte"))
    dish = _dish(conn, "Entrecôte frites")
    raw = actions.grammage_recovery(a, {dish: 0.28})
    assert raw == pytest.approx((0.30 - 0.28) / 0.9 * 31.50 * 400)   # 280 € d'économie…
    rec = actions.apply_grammage(conn, a, {dish: 0.28})
    assert rec == pytest.approx(-a.monthly_impact)                    # …comptés au plus 180 € récupérés
    q = conn.execute("SELECT quantity FROM dish_items WHERE dish_id = ? AND ingredient_id = ?",
                     (dish, a.ingredient_id)).fetchone()[0]
    assert q == 0.28


def test_recovery_summary(conn):
    r = actions.recovery_summary(conn)
    eggs = conn.execute("SELECT * FROM alert_actions").fetchone()      # traité dans la démo
    assert r.recovered == pytest.approx(eggs["recovered_monthly"]) and r.recovered > 0
    assert r.detected == pytest.approx(-eggs["monthly_impact"] + r.open)
    assert r.open == pytest.approx(sum(-a.monthly_impact for a in actions.open_alerts(conn)))


# --- Tarifs négociés / avoirs -------------------------------------------------------

def test_billing_errors_found(conn):
    errs = {e.ingredient: e for e in credits.billing_errors(conn)}
    assert set(errs) == {"Bœuf haché 15 %", "Beurre doux"}
    assert errs["Bœuf haché 15 %"].amount == pytest.approx((13.20 - 12.60) * 160)
    assert errs["Beurre doux"].amount == pytest.approx((10.40 - 10.00) * 20)


def test_negotiated_price_only_applies_from_its_date(conn):
    # le tarif bœuf date du 1er octobre : la facture de septembre (11,90 €) n'est pas en erreur
    # et celle d'octobre (12,60 € = tarif) non plus
    dates = [e.invoice_date for e in credits.billing_errors(conn, days=None) if e.ingredient == "Bœuf haché 15 %"]
    assert dates == ["2026-11-04"]


def test_claim_email_and_mark_claimed(conn):
    group = credits.group_by_invoice(credits.billing_errors(conn))[0]
    subject, body = credits.claim_email(group, "Brasserie du Marché")
    assert "Demande d'avoir" in subject and group[0].invoice_number in subject
    assert "tarif convenu" in body and body.rstrip().endswith("Brasserie du Marché")
    credits.mark_claimed(conn, group)
    left = credits.billing_errors(conn)
    assert all(e.line_id not in {g.line_id for g in group} for e in left)


# --- Factur-X ----------------------------------------------------------------------

CII = """<?xml version="1.0" encoding="UTF-8"?>
<rsm:CrossIndustryInvoice xmlns:rsm="urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100"
 xmlns:ram="urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100"
 xmlns:udt="urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100">
 <rsm:ExchangedDocument><ram:ID>FX-2026-118</ram:ID><ram:TypeCode>380</ram:TypeCode>
  <ram:IssueDateTime><udt:DateTimeString format="102">20261112</udt:DateTimeString></ram:IssueDateTime>
 </rsm:ExchangedDocument>
 <rsm:SupplyChainTradeTransaction>
  <ram:IncludedSupplyChainTradeLineItem>
   <ram:SpecifiedTradeProduct><ram:SellerAssignedID>402177</ram:SellerAssignedID>
    <ram:Name>CREME LIQUIDE 35% UHT 6X1L</ram:Name></ram:SpecifiedTradeProduct>
   <ram:SpecifiedLineTradeAgreement><ram:NetPriceProductTradePrice><ram:ChargeAmount>29.40</ram:ChargeAmount>
   </ram:NetPriceProductTradePrice></ram:SpecifiedLineTradeAgreement>
   <ram:SpecifiedLineTradeDelivery><ram:BilledQuantity unitCode="XPK">2</ram:BilledQuantity></ram:SpecifiedLineTradeDelivery>
   <ram:SpecifiedLineTradeSettlement><ram:SpecifiedTradeSettlementLineMonetarySummation>
    <ram:LineTotalAmount>58.80</ram:LineTotalAmount></ram:SpecifiedTradeSettlementLineMonetarySummation>
   </ram:SpecifiedLineTradeSettlement>
  </ram:IncludedSupplyChainTradeLineItem>
  <ram:IncludedSupplyChainTradeLineItem>
   <ram:SpecifiedTradeProduct><ram:Name>MESCLUN CAT1 1KG</ram:Name></ram:SpecifiedTradeProduct>
   <ram:SpecifiedLineTradeAgreement><ram:NetPriceProductTradePrice><ram:ChargeAmount>45.00</ram:ChargeAmount>
    <ram:BasisQuantity unitCode="KGM">5</ram:BasisQuantity></ram:NetPriceProductTradePrice></ram:SpecifiedLineTradeAgreement>
   <ram:SpecifiedLineTradeDelivery><ram:BilledQuantity unitCode="KGM">3</ram:BilledQuantity></ram:SpecifiedLineTradeDelivery>
   <ram:SpecifiedLineTradeSettlement><ram:SpecifiedTradeSettlementLineMonetarySummation>
    <ram:LineTotalAmount>27.00</ram:LineTotalAmount></ram:SpecifiedTradeSettlementLineMonetarySummation>
   </ram:SpecifiedLineTradeSettlement>
  </ram:IncludedSupplyChainTradeLineItem>
  <ram:ApplicableHeaderTradeAgreement><ram:SellerTradeParty><ram:Name>Pomona</ram:Name></ram:SellerTradeParty>
  </ram:ApplicableHeaderTradeAgreement>
  <ram:ApplicableHeaderTradeSettlement><ram:SpecifiedTradeSettlementHeaderMonetarySummation>
   <ram:TaxBasisTotalAmount>85.80</ram:TaxBasisTotalAmount></ram:SpecifiedTradeSettlementHeaderMonetarySummation>
  </ram:ApplicableHeaderTradeSettlement>
 </rsm:SupplyChainTradeTransaction>
</rsm:CrossIndustryInvoice>""".encode()


def test_parse_cii():
    inv = parse_cii(CII)
    assert (inv["supplier"], inv["invoice_number"], inv["invoice_date"], inv["total_ht"]) == \
        ("Pomona", "FX-2026-118", "2026-11-12", 85.80)
    creme, mesclun = inv["lines"]
    assert creme["unit"] == "COLIS" and creme["quantity"] == 2 and creme["unit_price_ht"] == 29.40
    assert creme["reference"] == "402177"
    assert mesclun["unit"] == "KG" and mesclun["unit_price_ht"] == pytest.approx(9.00)  # 45 € les 5 kg


def test_parse_cii_rejects_other_xml():
    with pytest.raises(FacturXError):
        parse_cii(b"<invoice/>")


def test_read_invoice_from_facturx_pdf_without_api(monkeypatch):
    from pypdf import PdfWriter
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    w.add_attachment("factur-x.xml", CII)
    buf = io.BytesIO()
    w.write(buf)
    inv = read_invoice(buf.getvalue(), "facture.pdf")
    assert inv["method"] == "facturx" and len(inv["lines"]) == 2
    assert all(not l["warning"] for l in inv["lines"])


def test_read_invoice_raw_xml():
    assert read_invoice(CII, "facture.xml")["supplier"] == "Pomona"


# --- E-mails -------------------------------------------------------------------------

def test_invoice_alert_message(conn):
    ids = {_ing(conn, "Bœuf haché 15 %")}
    subject, body = notify.invoice_alert(conn, ids)
    assert "1 hausse" in subject and "Bœuf haché" in body and "Prix conseillés" in body
    assert "tarifs négociés" in body       # l'écart sur le bœuf est aussi signalé


def test_invoice_alert_none_when_nothing(conn):
    assert notify.invoice_alert(conn, {_ing(conn, "Mesclun")}, set()) is None


def test_weekly_summary(conn):
    subject, body = notify.weekly_summary(conn)
    assert "à récupérer" in subject and "Déjà récupéré" in body and "Avoirs à demander" in body


def test_email_not_configured(monkeypatch):
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    assert not notify.email_configured()
    with pytest.raises(RuntimeError):
        notify.send_email("a@b.fr", "s", "b")
