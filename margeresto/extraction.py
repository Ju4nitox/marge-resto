"""Lecture d'une facture fournisseur (PDF ou photo) par l'API Claude."""
from __future__ import annotations

import base64
import os
from pathlib import Path

DEFAULT_MODEL = os.environ.get("MARGE_MODEL", "claude-sonnet-5-5")

MEDIA_TYPES = {
    ".xml": "application/xml",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

INVOICE_TOOL = {
    "name": "enregistrer_facture",
    "description": "Enregistre les données structurées d'une facture fournisseur de restaurant.",
    "input_schema": {
        "type": "object",
        "properties": {
            "supplier": {"type": "string", "description": "Nom du fournisseur (émetteur de la facture)"},
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string", "description": "Date de facture au format AAAA-MM-JJ"},
            "total_ht": {"type": ["number", "null"], "description": "Total HT de la facture"},
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Désignation telle qu'écrite"},
                        "reference": {"type": "string", "description": "Code article, vide si absent"},
                        "quantity": {"type": "number", "description": "Quantité facturée"},
                        "unit": {"type": "string", "description": "Unité facturée : KG, L, PCE, COLIS, CARTON..."},
                        "unit_price_ht": {"type": "number", "description": "Prix unitaire HT, après remise"},
                        "line_total_ht": {"type": ["number", "null"]},
                        "is_product": {
                            "type": "boolean",
                            "description": "false pour frais de port, consignes, remises globales, taxes",
                        },
                    },
                    "required": ["label", "quantity", "unit", "unit_price_ht", "is_product"],
                },
            },
        },
        "required": ["supplier", "invoice_number", "invoice_date", "lines"],
    },
}

PROMPT = (
    "Voici une facture d'un fournisseur de restaurant (France). Extrais toutes les lignes d'articles. "
    "Règles : montants HT en euros, nombres au format décimal (2.35 et non 2,35) ; "
    "si une remise ligne existe, unit_price_ht est le prix NET après remise ; "
    "garde la désignation exacte (elle contient souvent le conditionnement, ex. 6X1L, 10X250G) ; "
    "ne devine pas une ligne illisible, omets-la plutôt. Appelle l'outil enregistrer_facture."
)


class ExtractionError(RuntimeError):
    pass


def api_key_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def extract_invoice(file_bytes: bytes, filename: str, model: str = DEFAULT_MODEL) -> dict:
    """Renvoie {supplier, invoice_number, invoice_date, total_ht, lines:[...]}."""
    if not api_key_available():
        raise ExtractionError("Clé ANTHROPIC_API_KEY absente : renseigne-la dans le fichier .env.")
    ext = Path(filename).suffix.lower()
    media_type = MEDIA_TYPES.get(ext)
    if not media_type:
        raise ExtractionError(f"Format non pris en charge : {ext} (PDF, PNG, JPG, WEBP).")

    import anthropic

    data = base64.standard_b64encode(file_bytes).decode()
    block_type = "document" if media_type == "application/pdf" else "image"
    client = anthropic.Anthropic()
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=8000,
            tools=[INVOICE_TOOL],
            tool_choice={"type": "tool", "name": INVOICE_TOOL["name"]},
            messages=[{
                "role": "user",
                "content": [
                    {"type": block_type, "source": {"type": "base64", "media_type": media_type, "data": data}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        )
    except anthropic.APIError as e:
        raise ExtractionError(f"Erreur de l'API Claude : {e}") from e

    for block in msg.content:
        if block.type == "tool_use":
            return clean(block.input)
    raise ExtractionError("La réponse ne contient pas de facture structurée.")


def clean(raw: dict) -> dict:
    """Contrôles de cohérence simples ; les lignes douteuses sont signalées, pas supprimées."""
    lines = []
    for ln in raw.get("lines", []):
        q = float(ln.get("quantity") or 0)
        pu = float(ln.get("unit_price_ht") or 0)
        tot = ln.get("line_total_ht")
        warn = ""
        if q <= 0 or pu <= 0:
            warn = "quantité ou prix nul"
        elif tot is not None and abs(q * pu - float(tot)) > max(0.05, 0.02 * float(tot)):
            warn = f"quantité × prix ≠ total ({q * pu:.2f} vs {float(tot):.2f})"
        lines.append({
            "label": (ln.get("label") or "").strip(),
            "reference": (ln.get("reference") or "").strip(),
            "quantity": q,
            "unit": (ln.get("unit") or "").strip(),
            "unit_price_ht": pu,
            "line_total_ht": float(tot) if tot is not None else round(q * pu, 2),
            "is_product": bool(ln.get("is_product", True)),
            "warning": warn,
        })
    return {
        "supplier": (raw.get("supplier") or "").strip(),
        "invoice_number": str(raw.get("invoice_number") or "").strip(),
        "invoice_date": (raw.get("invoice_date") or "").strip(),
        "total_ht": raw.get("total_ht"),
        "lines": lines,
    }


def read_invoice(file_bytes: bytes, filename: str, model: str = DEFAULT_MODEL) -> dict:
    """Point d'entrée unique : Factur-X d'abord (gratuit, exact), sinon lecture par IA.

    Ajoute "method" : "facturx" ou "ia".
    """
    from .facturx import FacturXError, find_xml_in_pdf, parse_cii

    ext = Path(filename).suffix.lower()
    xml = file_bytes if ext == ".xml" else (find_xml_in_pdf(file_bytes) if ext == ".pdf" else None)
    if xml is not None:
        try:
            out = clean(parse_cii(xml))
            out["method"] = "facturx"
            return out
        except FacturXError as e:
            if ext == ".xml":
                raise ExtractionError(str(e)) from e
    out = extract_invoice(file_bytes, filename, model)
    out["method"] = "ia"
    return out
