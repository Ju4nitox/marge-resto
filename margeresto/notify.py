"""Alertes par e-mail : à la validation d'une facture, et résumé de la semaine.

Configuration (fichier .env ou « Secrets » Streamlit) :
    SMTP_HOST, SMTP_PORT (587 par défaut), SMTP_USER, SMTP_PASSWORD, SMTP_FROM (optionnel)
Le destinataire se règle dans Paramètres.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

from . import actions, credits
from .db import get_text


def _fr(x: float, dec: int = 0) -> str:
    return f"{x:,.{dec}f}".replace(",", " ").replace(".", ",") + " €"


def email_configured() -> bool:
    return all(os.environ.get(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"))


def send_email(to: str, subject: str, body: str) -> None:
    if not email_configured():
        raise RuntimeError("Envoi d'e-mails non configuré (SMTP_HOST, SMTP_USER, SMTP_PASSWORD).")
    msg = EmailMessage()
    msg["From"] = os.environ.get("SMTP_FROM") or os.environ["SMTP_USER"]
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], port, context=ctx, timeout=20) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(os.environ["SMTP_HOST"], port, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
            s.send_message(msg)


def _alert_lines(alerts: list[actions.OpenAlert]) -> list[str]:
    out = []
    for a in alerts:
        sugg = actions.suggested_prices(a)
        names = {d.dish_id: d for d in a.dishes}
        line = (f"- {a.name} : +{a.change * 100:.1f} % chez {a.supplier} "
                f"({a.price_before:.2f} → {a.price_after:.2f} €/{a.base_unit}), "
                f"{_fr(-a.monthly_impact)} de marge en moins par mois").replace(".", ",")
        if sugg:
            line += "\n  Prix conseillés : " + ", ".join(
                f"{names[i].dish} {names[i].price_ttc:.2f} → {p:.2f} €".replace(".", ",") for i, p in sugg.items())
        out.append(line)
    return out


def invoice_alert(conn, ingredient_ids: set[int], invoice_line_ids: set[int] | None = None):
    """Message à envoyer après une facture : hausses et écarts qui la concernent. None si rien."""
    alerts = [a for a in actions.open_alerts(conn) if a.ingredient_id in ingredient_ids]
    errors = [e for e in credits.billing_errors(conn, days=None)
              if invoice_line_ids is None or e.line_id in invoice_line_ids]
    if not alerts and not errors:
        return None
    parts = []
    if alerts:
        parts += ["Hausses détectées sur cette facture :", "", *_alert_lines(alerts), ""]
    if errors:
        parts += ["Prix au-dessus de tes tarifs négociés (demande un avoir) :", ""]
        parts += [f"- {e.ingredient} chez {e.supplier} : {e.paid:.2f} au lieu de {e.agreed:.2f} €/{e.base_unit}, "
                  f"soit {e.amount:.2f} € HT".replace(".", ",") for e in errors]
        parts.append("")
    loss = sum(-a.monthly_impact for a in alerts)
    subject = (f"Marge Resto : {len(alerts)} hausse{'s' if len(alerts) > 1 else ''}, "
               f"{_fr(loss)} par mois" if alerts else
               f"Marge Resto : {len(errors)} écart{'s' if len(errors) > 1 else ''} de facturation")
    parts.append("Ouvre Marge Resto pour traiter ces alertes.")
    return subject, "Bonjour,\n\n" + "\n".join(parts) + "\n"


def weekly_summary(conn):
    rec = actions.recovery_summary(conn)
    alerts = actions.open_alerts(conn)
    errors = credits.billing_errors(conn)
    name = get_text(conn, "restaurant_name") or "ton restaurant"
    subject = f"Marge Resto : ta semaine, {_fr(rec.open)} de marge à récupérer par mois"
    body = ["Bonjour,", "", f"Le point sur les achats de {name} :", "",
            f"- Hausses détectées (90 jours) : {_fr(rec.detected)} par mois",
            f"- Déjà récupéré : {_fr(rec.recovered)} par mois",
            f"- Reste à traiter : {_fr(rec.open)} par mois", ""]
    if alerts:
        body += ["À traiter en priorité :", "", *_alert_lines(alerts[:3]), ""]
    if errors:
        body += [f"Avoirs à demander : {_fr(sum(e.amount for e in errors), 2)} HT "
                 f"sur {len(credits.group_by_invoice(errors))} facture(s).", ""]
    body.append("Ouvre Marge Resto pour agir.")
    return subject, "\n".join(body) + "\n"
