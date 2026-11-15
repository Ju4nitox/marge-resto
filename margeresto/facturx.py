"""Lecture des factures électroniques Factur-X / ZUGFeRD (XML CII), sans IA.

Les grands grossistes émettent des factures électroniques depuis septembre 2026 :
le PDF contient un fichier XML structuré (factur-x.xml) qu'on lit directement,
gratuitement et sans erreur de lecture.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET

NS = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}

# Codes d'unité UN/ECE rec. 20 -> libellé lisible
UNIT_CODES = {
    "KGM": "KG", "GRM": "G", "LTR": "L", "CLT": "CL", "MLT": "ML",
    "H87": "PCE", "C62": "PCE", "EA": "PCE", "XPC": "PCE",
    "XCT": "CARTON", "CT": "CARTON", "XBX": "BOITE", "XPK": "COLIS", "PK": "COLIS", "XCS": "COLIS",
    "XBG": "SAC", "XSA": "SAC", "XBO": "BOUTEILLE", "XCA": "BIDON", "XTN": "BIDON",
}

ATTACHMENT_NAMES = ("factur-x.xml", "zugferd-invoice.xml", "xrechnung.xml")


class FacturXError(ValueError):
    pass


def find_xml_in_pdf(pdf_bytes: bytes) -> bytes | None:
    """Renvoie le XML Factur-X embarqué dans le PDF, ou None si le PDF est une facture « papier »."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        attachments = reader.attachments
    except Exception:
        return None
    for name, contents in attachments.items():
        if name.lower() in ATTACHMENT_NAMES or name.lower().endswith(".xml"):
            if contents:
                return contents[0]
    return None


def _text(el, path: str) -> str:
    found = el.find(path, NS)
    return found.text.strip() if found is not None and found.text else ""


def _num(el, path: str) -> float | None:
    t = _text(el, path)
    try:
        return float(t) if t else None
    except ValueError:
        return None


def parse_cii(xml_bytes: bytes) -> dict:
    """XML CII (Factur-X) -> même structure que la lecture par IA."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise FacturXError(f"XML illisible : {e}") from e
    if not root.tag.endswith("CrossIndustryInvoice"):
        raise FacturXError("Ce XML n'est pas une facture Factur-X (CII).")

    tx = root.find("rsm:SupplyChainTradeTransaction", NS)
    if tx is None:
        raise FacturXError("Facture sans transaction.")
    raw_date = _text(root, "rsm:ExchangedDocument/ram:IssueDateTime/udt:DateTimeString")
    inv_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) >= 8 else ""
    summ = "ram:ApplicableHeaderTradeSettlement/ram:SpecifiedTradeSettlementHeaderMonetarySummation/"
    total = _num(tx, summ + "ram:TaxBasisTotalAmount")
    if total is None:
        total = _num(tx, summ + "ram:LineTotalAmount")

    lines = []
    for item in tx.findall("ram:IncludedSupplyChainTradeLineItem", NS):
        qty_el = item.find("ram:SpecifiedLineTradeDelivery/ram:BilledQuantity", NS)
        qty = float(qty_el.text) if qty_el is not None and qty_el.text else 0.0
        unit_code = qty_el.get("unitCode", "") if qty_el is not None else ""
        price = _num(item, "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:ChargeAmount") or 0.0
        basis = _num(item, "ram:SpecifiedLineTradeAgreement/ram:NetPriceProductTradePrice/ram:BasisQuantity")
        if basis and basis > 0:
            price = price / basis            # prix exprimé pour N unités
        line_total = _num(item, "ram:SpecifiedLineTradeSettlement/"
                                "ram:SpecifiedTradeSettlementLineMonetarySummation/ram:LineTotalAmount")
        lines.append({
            "label": _text(item, "ram:SpecifiedTradeProduct/ram:Name"),
            "reference": _text(item, "ram:SpecifiedTradeProduct/ram:SellerAssignedID"),
            "quantity": qty,
            "unit": UNIT_CODES.get(unit_code, unit_code),
            "unit_price_ht": round(price, 4),
            "line_total_ht": line_total,
            "is_product": True,
        })
    return {
        "supplier": _text(tx, "ram:ApplicableHeaderTradeAgreement/ram:SellerTradeParty/ram:Name"),
        "invoice_number": _text(root, "rsm:ExchangedDocument/ram:ID"),
        "invoice_date": inv_date,
        "total_ht": total,
        "lines": lines,
    }
