"""Marge Resto — prototype Streamlit.

Le problème : les prix fournisseurs montent par petites touches, les prix de la carte
ne bougent pas, et la marge fond sans qu'on le voie. L'app détecte chaque hausse dès
la facture, la chiffre sur tous les plats concernés, et propose quoi faire.

Lancer :  streamlit run app.py
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

from margeresto import actions, credits, db, demo, notify, pricing, suppliers
from margeresto.extraction import ExtractionError, api_key_available, read_invoice
from margeresto.matching import base_qty_from_unit, suggest_ingredient

ROOT = Path(__file__).resolve().parent
IGNORE = "— ignorer (frais, consigne…) —"
PAGES: dict = {}
SECRET_KEYS = ("ANTHROPIC_API_KEY", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")


# --- Mise en forme -------------------------------------------------------------

def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    try:  # « Secrets » Streamlit Cloud
        for k in SECRET_KEYS:
            if k in st.secrets:
                os.environ.setdefault(k, str(st.secrets[k]))
    except Exception:
        pass


def eur(x: float, dec: int = 2, sign: bool = False) -> str:
    s = f"{x:+,.{dec}f}" if sign else f"{x:,.{dec}f}"
    return s.replace(",", " ").replace(".", ",").replace("-", "−") + " €"


def pct(x: float, dec: int = 1, sign: bool = False) -> str:
    s = f"{x * 100:+.{dec}f}" if sign else f"{x * 100:.{dec}f}"
    return s.replace(".", ",").replace("-", "−") + " %"


def qty(q: float, unit: str) -> str:
    """0.18 kg -> '180 g', 0.03 L -> '3 cl', 2 pièce -> '2 pièces'."""
    if unit == "kg" and q < 1:
        return f"{q * 1000:.0f} g"
    if unit == "L" and q < 1:
        return f"{q * 100:.0f} cl"
    if unit == "pièce":
        return f"{q:,.0f} pièce".replace(",", " ") + ("s" if q > 1 else "")
    if q >= 10:
        return f"{q:,.0f} {unit}".replace(",", " ")
    return f"{q:.1f} {unit}".replace(".", ",")


CSS = """
<style>
/* Polices : déclarées dans .streamlit/config.toml (servies depuis static/fonts, licence OFL) */

:root {
  --bouteille: #1F4D3A;   /* banquettes */
  --bouteille-2: #2A5E49;
  --laiton: #D9B44A;      /* comptoir */
  --carrelage: #FAFBF7;
  --encre: #1C2421;
  --tomate: #C8432F;      /* pertes */
  --herbe: #2D7A55;       /* gains */
  --sauge: #6B7A72;
  --filet: #DCE5DD;
}
h1, h2, h3, h4 { letter-spacing: -0.02em; color: var(--encre); }
h1 { font-weight: 800 !important; font-size: 2.3rem !important; padding-bottom: .4rem !important; }
h3 { font-weight: 700 !important; font-size: 1.25rem !important; margin-top: 1.6rem !important; }
.block-container { max-width: 1040px; padding-top: 2.4rem; padding-bottom: 4rem; }

/* L'ardoise : le chiffre clé de chaque page, sur fond vert bouteille carrelé */
.slate {
  position: relative; overflow: hidden;
  background-color: var(--bouteille);
  background-image:
    linear-gradient(rgba(255,255,255,.045) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255,255,255,.045) 1px, transparent 1px);
  background-size: 26px 13px;   /* carreaux métro */
  color: #F3F6F1; border-radius: 16px; padding: 1.7rem 2rem 1.6rem;
  border-top: 5px solid var(--laiton); margin: .4rem 0 1.4rem;
}
.slate-label { font-size: 1.02rem; opacity: .86; max-width: 40ch; }
.slate-number {
  font-family: 'Bricolage Grotesque', sans-serif; font-weight: 800;
  font-size: clamp(2.6rem, 7vw, 4.2rem); line-height: 1; letter-spacing: -0.03em;
  margin: .45rem 0 .5rem; font-variant-numeric: tabular-nums;
}
.slate.loss .slate-number { color: #FFB4A3; }
.slate.gain .slate-number { color: var(--laiton); }
.slate-sub { font-size: .95rem; opacity: .78; max-width: 62ch; }

/* Cartes : blanches, filet sauge, pas d'ombre */
[data-testid="stVerticalBlockBorderWrapper"]:has(> div > [data-testid="stVerticalBlock"]) {
  background: #FFFFFF;
}
div[data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--filet) !important; }

/* Chiffres */
[data-testid="stMetricValue"] { font-family: 'Bricolage Grotesque', sans-serif; font-weight: 700; font-variant-numeric: tabular-nums; }
[data-testid="stMetricLabel"] p { color: var(--sauge); font-weight: 600; }
.num { font-variant-numeric: tabular-nums; }
.red { color: var(--tomate); } .green { color: var(--herbe); } .brass { color: #9A7618; }
.muted { color: var(--sauge); font-size: .9rem; }
.big { font-family: 'Bricolage Grotesque', sans-serif; font-size: 1.7rem; font-weight: 800; line-height: 1.1; }
.pill { display: inline-block; padding: .08rem .55rem; border-radius: 999px; font-weight: 700; font-size: .9rem; }
.pill.loss { background: #FBE6E1; color: var(--tomate); }
.pill.gain { background: #E2F0E7; color: var(--herbe); }

/* Boutons */
.stButton > button[kind="primary"], .stFormSubmitButton > button { background: var(--bouteille); border-color: var(--bouteille); }
.stButton > button[kind="primary"]:hover { background: var(--bouteille-2); border-color: var(--bouteille-2); }
.stButton > button:focus-visible { outline: 3px solid var(--laiton); outline-offset: 2px; }

/* Barre latérale */
[data-testid="stSidebar"] h1, [data-testid="stSidebar"] h2 { color: #F3F6F1; }
[data-testid="stSidebarNav"] a span { font-weight: 600; }

@media (max-width: 640px) {
  .slate { padding: 1.3rem 1.2rem 1.2rem; }
  h1 { font-size: 1.8rem !important; }
}
</style>
"""


def slate(label: str, number: str, sub: str = "", tone: str = "loss") -> None:
    """Affiche le chiffre clé d'une page sur l'« ardoise » vert bouteille."""
    st.markdown(f"<div class='slate {tone}'><div class='slate-label'>{label}</div>"
                f"<div class='slate-number'>{number}</div>"
                + (f"<div class='slate-sub'>{sub}</div>" if sub else "") + "</div>",
                unsafe_allow_html=True)


@st.cache_resource
def get_conn():
    return db.connect(os.environ.get("MARGE_DB", db.DEFAULT_DB))


def ingredients(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ingredients ORDER BY name")]


def empty_state() -> None:
    st.info("Pas encore de données. Va dans **Paramètres** et clique sur "
            "« Charger la brasserie de démonstration » pour essayer.")


def go_product(ingredient_id: int) -> None:
    st.session_state["produit"] = ingredient_id
    st.switch_page(PAGES["produit"])


def toast_done() -> None:
    msg = st.session_state.pop("done", None)
    if msg:
        st.success(msg)


# --- Alertes (accueil) ---------------------------------------------------------

def page_alerts():
    conn = get_conn()
    st.title("Tes alertes")
    toast_done()
    if not conn.execute("SELECT 1 FROM invoices LIMIT 1").fetchone():
        empty_state()
        return
    alerts = actions.open_alerts(conn)
    rec = actions.recovery_summary(conn)
    errors = credits.billing_errors(conn)

    if alerts:
        slate("Marge à récupérer chaque mois", eur(rec.open, 0),
              f"{len(alerts)} hausse{'s' if len(alerts) > 1 else ''} à traiter. "
              f"Déjà récupéré : {eur(rec.recovered, 0)} par mois sur {eur(rec.detected, 0)} de hausses "
              "détectées ces 90 derniers jours.")
    else:
        slate("Toutes les hausses sont traitées. Marge récupérée chaque mois", eur(rec.recovered, 0),
              f"Sur {eur(rec.detected, 0)} de hausses détectées ces 90 derniers jours.", "gain")

    if rec.detected > 0:
        share = min(rec.recovered / rec.detected, 1.0)
        st.progress(share, text=f"{pct(share, 0)} de la marge menacée déjà récupérée")

    st.subheader("À traiter")
    if not alerts:
        st.success("Rien à traiter. La prochaine facture sera vérifiée automatiquement.")
    for a in alerts:
        with st.container(border=True):
            x, y, z = st.columns([3, 3, 1], vertical_alignment="center")
            n = len(a.dishes)
            x.markdown(f"**{a.name}**  \n<span class='muted'>chez {a.supplier}, "
                       + (f"dans {n} plat{'s' if n > 1 else ''}" if n else "dans aucun plat") + "</span>",
                       unsafe_allow_html=True)
            y.markdown(f"<span class='pill loss'>{pct(a.change, sign=True)}</span> "
                       f"<span class='red num'><b>{eur(a.monthly_impact, 0)}</b> par mois</span>",
                       unsafe_allow_html=True)
            z.button("Traiter", key=f"todo_{a.ingredient_id}", on_click=go_product,
                     args=(a.ingredient_id,), type="primary", width="stretch")

    if errors:
        st.subheader("Avoirs à demander")
        st.caption("Ces prix dépassent les tarifs que tu as négociés : ce n'est pas une hausse, "
                   "c'est une erreur de facturation à te faire rembourser.")
        name = db.get_text(conn, "restaurant_name")
        for group in credits.group_by_invoice(errors):
            first, total = group[0], sum(e.amount for e in group)
            subject, body = credits.claim_email(group, name)
            with st.container(border=True):
                x, y = st.columns([4, 2], vertical_alignment="center")
                d = date.fromisoformat(first.invoice_date).strftime("%d/%m/%Y")
                x.markdown(f"**{first.supplier}**, facture n° {first.invoice_number} du {d}  \n"
                           f"<span class='muted'>{', '.join(e.ingredient for e in group)}</span>",
                           unsafe_allow_html=True)
                y.markdown(f"<span class='big brass'>{eur(total)}</span> <span class='muted'>HT</span>",
                           unsafe_allow_html=True)
                with st.expander("Voir l'e-mail de demande d'avoir"):
                    st.code(f"Objet : {subject}\n\n{body}", language=None, wrap_lines=True)
                    b1, b2 = st.columns(2)
                    b1.link_button("Ouvrir dans ma messagerie",
                                   f"mailto:?subject={quote(subject)}&body={quote(body)}", width="stretch")
                    if b2.button("C'est envoyé", key=f"claim_{first.invoice_id}", width="stretch"):
                        credits.mark_claimed(conn, group)
                        st.session_state["done"] = f"Demande d'avoir de {eur(total)} notée."
                        st.rerun()

    if rec.handled:
        with st.expander(f"Traitées ces 90 derniers jours ({len(rec.handled)})"):
            for h in rec.handled:
                d = date.fromisoformat(h["handled_on"]).strftime("%d/%m")
                st.markdown(f"**{h['name']}** : {actions.KINDS[h['kind']].lower()}"
                            + (f", {eur(h['recovered_monthly'], 0)} par mois récupérés" if h["recovered_monthly"] else "")
                            + f" <span class='muted'>(le {d}{', ' + h['detail'] if h['detail'] else ''})</span>",
                            unsafe_allow_html=True)


# --- Par produit -------------------------------------------------------------------

def page_product():
    conn = get_conn()
    st.title("Par produit")
    toast_done()
    statuses = [p for p in pricing.all_price_status(conn) if p.current is not None]
    if not statuses:
        empty_state()
        return
    alerts = {a.ingredient_id: a for a in actions.open_alerts(conn)}
    # produits en alerte d'abord, puis par ordre alphabétique
    order = list(alerts) + [p.ingredient_id for p in statuses if p.ingredient_id not in alerts]
    names = {p.ingredient_id: p.name for p in statuses}
    default = st.session_state.get("produit", order[0])
    pick = st.selectbox("Produit", order, index=order.index(default) if default in order else 0,
                        format_func=lambda i: f"{names[i]}  (à traiter)" if i in alerts else names[i],
                        label_visibility="collapsed")
    st.session_state["produit"] = pick
    alert = alerts.get(pick)

    if alert:
        slate(f"{alert.name} : le prix est passé de {eur(alert.price_before)} à {eur(alert.price_after)} "
              f"le {alert.base_unit} ({pct(alert.change, sign=True)}). Ce que ça te coûte chaque mois :",
              eur(alert.monthly_impact, 0, sign=True),
              f"Soit {eur(alert.monthly_impact * 12, 0, sign=True)} sur un an. Dernier achat chez {alert.supplier}.")
        action_plan(conn, alert)
        dishes, unit = alert.dishes, alert.base_unit
    else:
        imp = pricing.ingredient_impact(conn, pick)
        s, dishes, unit = imp["status"], imp["dishes"], imp["status"].base_unit
        la = actions.last_action(conn, pick)
        sub = (f"Dernier traitement : {actions.KINDS[la['kind']].lower()} le "
               f"{date.fromisoformat(la['handled_on']).strftime('%d/%m')}." if la else
               "Pas de hausse à traiter sur ce produit.")
        slate(f"{s.name} : dernier prix payé chez {s.supplier}", f"{eur(s.current)} / {unit}", sub, "gain")

    with st.expander("Et si le prix bougeait ? (simulation)"):
        sim = st.slider("Variation du prix à partir du prix actuel", -30, 50, 10, format="%d %%") / 100
        simp = pricing.ingredient_impact(conn, pick, sim)
        total = sum(d.monthly_impact for d in simp["dishes"])
        st.markdown(f"À {eur(simp['price_after'])} le {unit}, ta marge bougerait de "
                    f"**{eur(total, 0, sign=True)} par mois** ({eur(total * 12, 0, sign=True)} sur un an).")

    if dishes:
        st.subheader(f"Dans tes plats ({len(dishes)})")
        for d in sorted(dishes, key=lambda d: d.monthly_impact):
            with st.container(border=True):
                x, y = st.columns([3, 2], vertical_alignment="center")
                x.markdown(f"**{d.dish}**  \n<span class='muted'>{qty(d.gross_qty, unit)} par assiette, "
                           f"{d.monthly_volume:.0f} vendus par mois, {eur(d.price_ttc)} TTC</span>",
                           unsafe_allow_html=True)
                if alert:
                    y.markdown(f"<span class='red num'><b>{eur(d.monthly_impact, 0, sign=True)}</b> par mois</span>  \n"
                               f"<span class='muted'>marge {pct(d.margin_rate_before)} → {pct(d.margin_rate_after)}</span>",
                               unsafe_allow_html=True)
                else:
                    y.markdown(f"<span class='muted'>marge</span> **{pct(d.margin_rate_after)}**",
                               unsafe_allow_html=True)
    else:
        st.info("Ce produit n'entre dans aucun plat. Ajoute-le à une fiche dans **Ma carte**.")

    suppliers_block(conn, pick, unit)

    hist = pricing.price_history(conn, pick)
    if len(hist) > 1:
        with st.expander("Historique du prix"):
            h = pd.DataFrame(hist)
            h["date"] = pd.to_datetime(h["date"])
            st.line_chart(h.groupby("date")["price"].mean(), y_label=f"€ HT / {unit}", height=220)


def action_plan(conn, alert: actions.OpenAlert):
    st.subheader("Que faire ?")
    errs = [e for e in credits.billing_errors(conn) if e.ingredient == alert.name]
    if errs:
        e = errs[0]
        st.warning(f"Attention : {e.supplier} t'a facturé {eur(e.paid)} au lieu de ton tarif négocié de "
                   f"{eur(e.agreed)} le {e.base_unit}. C'est peut-être une erreur plutôt qu'une hausse : "
                   "demande d'abord un avoir depuis **Alertes**.")
    loss = -alert.monthly_impact
    t1, t2, t3, t4 = st.tabs(["Ajuster mes prix", "Changer de fournisseur", "Réduire la portion", "Absorber"])

    with t1:
        sugg = actions.suggested_prices(alert)
        if not alert.dishes:
            st.write("Ce produit n'entre dans aucun plat.")
        else:
            st.caption("Prix conseillé : garde la même marge en euros par assiette, arrondi aux 10 centimes. "
                       "Les plats où la hausse pèse moins de 5 centimes ne sont pas touchés.")
            new = {}
            for d in sorted(alert.dishes, key=lambda d: d.monthly_impact):
                x, y = st.columns([3, 2], vertical_alignment="center")
                x.markdown(f"**{d.dish}**  \n<span class='muted'>aujourd'hui {eur(d.price_ttc)}</span>",
                           unsafe_allow_html=True)
                v = y.number_input("Nouveau prix TTC", value=float(sugg.get(d.dish_id, d.price_ttc)),
                                   min_value=0.0, step=0.10, format="%.2f", key=f"px_{alert.ingredient_id}_{d.dish_id}")
                if abs(v - d.price_ttc) > 0.001:
                    new[d.dish_id] = round(v, 2)
            gain = actions.reprice_recovery(alert, new)
            effect_line(gain, loss)
            if st.button("Appliquer ces prix", type="primary", disabled=not new, key="do_reprice"):
                r = actions.apply_reprice(conn, alert, new)
                st.session_state["done"] = f"Prix mis à jour : {eur(r, 0)} de marge récupérés par mois."
                st.rerun()

    with t2:
        opt = actions.supplier_option(conn, alert)
        if opt:
            b = opt.best
            st.markdown(f"**{b.supplier}** te le vend {eur(b.price)} le {alert.base_unit} au lieu de "
                        f"{eur(opt.current.price)} ({pct(b.price / opt.current.price - 1, sign=True)}), "
                        f"d'après {'ta facture' if b.source == 'facture' else 'son devis'} du "
                        f"{date.fromisoformat(b.date).strftime('%d/%m')}.")
            effect_line(opt.monthly_saving, loss)
            if st.button(f"Je passe chez {b.supplier}", type="primary", key="do_supplier"):
                r = actions.apply_supplier(conn, alert, opt)
                st.session_state["done"] = f"Changement noté : {eur(r, 0)} de marge récupérés par mois."
                st.rerun()
        else:
            st.write("Aucun fournisseur moins cher connu pour ce produit, avec un prix de moins de 2 mois.")
            st.caption("Demande un devis à un autre fournisseur et ajoute son prix plus bas, dans "
                       "« Chez tes fournisseurs ».")

    with t3:
        if not alert.dishes:
            st.write("Ce produit n'entre dans aucun plat.")
        else:
            st.caption("À réserver aux plats où la portion reste généreuse. Quantité nette dans l'assiette.")
            new_q = {}
            for d in sorted(alert.dishes, key=lambda d: d.monthly_impact):
                x, y = st.columns([3, 2], vertical_alignment="center")
                x.markdown(f"**{d.dish}**  \n<span class='muted'>aujourd'hui {qty(d.net_qty, alert.base_unit)}</span>",
                           unsafe_allow_html=True)
                step = 0.005 if alert.base_unit in ("kg", "L") else 1.0
                v = y.number_input(f"Nouvelle quantité ({alert.base_unit})", value=float(d.net_qty), min_value=0.0,
                                   step=step, format="%.3f" if step < 1 else "%.0f",
                                   key=f"q_{alert.ingredient_id}_{d.dish_id}")
                if abs(v - d.net_qty) > 1e-9:
                    new_q[d.dish_id] = v
            gain = actions.grammage_recovery(alert, new_q)
            effect_line(gain, loss)
            if st.button("Appliquer ces portions", type="primary", disabled=not new_q, key="do_grammage"):
                r = actions.apply_grammage(conn, alert, new_q)
                st.session_state["done"] = f"Fiches mises à jour : {eur(r, 0)} de marge récupérés par mois."
                st.rerun()

    with t4:
        st.write(f"Tu gardes tes prix et tes recettes : ta marge baisse de {eur(loss, 0)} par mois "
                 f"({eur(loss * 12, 0)} sur un an). L'alerte se rouvrira si le prix monte encore.")
        if st.button("Absorber cette hausse", key="do_absorb"):
            actions.absorb(conn, alert)
            st.session_state["done"] = f"Hausse sur {alert.name} absorbée."
            st.rerun()


def effect_line(gain: float, loss: float) -> None:
    if gain <= 0:
        st.markdown("<span class='muted'>Modifie une valeur pour voir l'effet.</span>", unsafe_allow_html=True)
        return
    if gain >= loss:
        st.markdown(f"<span class='brass'><b>Toute la perte est récupérée</b></span> ({eur(loss, 0)} par mois)"
                    + (f", et {eur(gain - loss, 0)} de marge en plus." if gain - loss >= 1 else "."),
                    unsafe_allow_html=True)
    else:
        st.markdown(f"Marge récupérée : <span class='brass'><b>{eur(gain, 0)} par mois</b></span> "
                    f"sur {eur(loss, 0)} perdus ({pct(gain / loss, 0)}).", unsafe_allow_html=True)


def suppliers_block(conn, pick: int, unit: str):
    st.subheader("Chez tes fournisseurs")
    cmp_ = suppliers.compare_ingredient(conn, pick)
    if cmp_:
        for o in cmp_.offers:
            best = o is cmp_.best and cmp_.has_alternative
            st.markdown(("🏆 " if best else "• ") + _offer_line(o, unit, cmp_, current=o.supplier == cmp_.current.supplier)
                        + (" <span class='muted'>(ton fournisseur actuel)</span>"
                           if o.supplier == cmp_.current.supplier else ""), unsafe_allow_html=True)
    neg = credits.negotiated_list(conn, pick)
    if neg:
        st.markdown("Tarifs négociés : " + ", ".join(
            f"**{n['supplier']}** {eur(n['price'])} le {unit} <span class='muted'>(depuis le "
            f"{date.fromisoformat(n['valid_from']).strftime('%d/%m/%Y')})</span>" for n in neg),
            unsafe_allow_html=True)
    c1, c2 = st.columns(2)
    with c1.popover("+ Prix d'un autre fournisseur", width="stretch"):
        quote_form(conn, key="prod", ingredient_id=pick)
    with c2.popover("+ Tarif négocié", width="stretch"):
        negotiated_form(conn, pick, unit)


def negotiated_form(conn, ingredient_id: int, unit: str):
    st.caption("Le prix convenu avec ton fournisseur. Chaque facture au-dessus sera signalée "
               "comme une erreur, avec un e-mail de demande d'avoir prêt à envoyer.")
    known = [r["name"] for r in conn.execute("SELECT name FROM suppliers ORDER BY name")]
    with st.form(f"neg_{ingredient_id}", clear_on_submit=True, border=False):
        sup = st.selectbox("Fournisseur", known) if known else st.text_input("Fournisseur")
        price = st.number_input(f"Tarif HT par {unit}", min_value=0.0, step=0.1, format="%.2f")
        since = st.date_input("Valable depuis le", date.today(), format="DD/MM/YYYY")
        if st.form_submit_button("Enregistrer le tarif"):
            if not sup or price <= 0:
                st.error("Indique le fournisseur et le tarif.")
            else:
                credits.set_negotiated(conn, ingredient_id, sup, price, since.isoformat())
                st.session_state["done"] = "Tarif négocié enregistré."
                st.rerun()


# --- Fournisseurs : prix comparés et devis ---------------------------------------

def _offer_line(o, unit: str, cmp_, current: bool = False) -> str:
    when = date.fromisoformat(o.date).strftime("%d/%m")
    tag = f"{'facture' if o.source == 'facture' else 'devis'} du {when}"
    stale = " <span class='pill loss'>à confirmer</span>" if cmp_.is_stale(o) and not current else ""
    return f"**{o.supplier}**, {eur(o.price)} le {unit} <span class='muted'>({tag})</span>{stale}"


def quote_form(conn, key: str, ingredient_id: int | None = None):
    ings = ingredients(conn)
    if not ings:
        return
    by_id = {i["id"]: i for i in ings}
    known = [r["name"] for r in conn.execute("SELECT name FROM suppliers ORDER BY name")]
    with st.form(f"quote_{key}", clear_on_submit=True):
        c1, c2 = st.columns(2)
        ids = [i["id"] for i in ings]
        ing = c1.selectbox("Produit", ids, index=ids.index(ingredient_id) if ingredient_id in ids else 0,
                           format_func=lambda i: by_id[i]["name"], disabled=ingredient_id is not None)
        sup = c2.text_input("Fournisseur", placeholder=", ".join(known[:3]) if known else "ex. Promocash")
        c3, c4, c5 = st.columns([1, 1, 2])
        price = c3.number_input(f"Prix HT par {by_id[ing]['base_unit']}", min_value=0.0, step=0.1, format="%.2f")
        when = c4.date_input("Date", date.today(), format="DD/MM/YYYY")
        note = c5.text_input("Note (optionnel)", placeholder="devis, catalogue, franco…")
        sure = st.checkbox("Je confirme ce prix même s'il est très éloigné de mon prix actuel")
        if st.form_submit_button("Ajouter ce prix"):
            target = ingredient_id or ing
            cur = pricing.price_status(conn, target).current
            if not sup.strip() or price <= 0:
                st.error("Indique le fournisseur et un prix.")
            elif cur and not (0.5 <= price / cur <= 2) and not sure:
                st.error(f"Ce prix est très loin de ton prix actuel ({eur(cur)}/{by_id[target]['base_unit']}). "
                         f"Vérifie que c'est bien un prix par {by_id[target]['base_unit']}, puis coche la case.")
            else:
                db.add_quote(conn, target, sup, price, when.isoformat(), note)
                st.rerun()


# --- Factures -----------------------------------------------------------------

def _prefill_lines(conn, supplier: str, raw_lines: list[dict]) -> pd.DataFrame:
    ings = ingredients(conn)
    by_id = {i["id"]: i for i in ings}
    rows = []
    for ln in raw_lines:
        ing_name, bq, status = IGNORE, None, "⚪"
        if ln.get("is_product", True):
            known = db.known_mapping(conn, supplier, ln["label"]) if supplier else None
            if known:
                ing = by_id[known["ingredient_id"]]
                ing_name, bq, status = ing["name"], known["base_qty_per_unit"], "✅"
            else:
                sugg = suggest_ingredient(ln["label"], ings, limit=1)
                if sugg and sugg[0][1] >= 70:
                    ing = sugg[0][0]
                    ing_name = ing["name"]
                    bq = base_qty_from_unit(ln.get("unit", ""), ln["label"], ing["base_unit"])
                    status = "🟡"
                else:
                    status = "🔴"
        if ln.get("warning"):
            status += " ⚠"
        rows.append({"": status, "Désignation": ln["label"], "Qté": ln["quantity"], "Unité": ln.get("unit", ""),
                     "Prix unitaire HT": ln["unit_price_ht"], "Produit": ing_name, "kg/L/pièces par unité": bq})
    return pd.DataFrame(rows)


def _new_ingredient_form(conn, key: str):
    with st.popover("+ Nouveau produit"):
        with st.form(f"new_ing_{key}", clear_on_submit=True, border=False):
            name = st.text_input("Nom", placeholder="ex. Beurre doux")
            unit = st.selectbox("Acheté au", db.BASE_UNITS)
            if st.form_submit_button("Créer") and name.strip():
                db.get_or_create_ingredient(conn, name, unit)
                conn.commit()
                st.rerun()


def page_invoices():
    conn = get_conn()
    st.title("Factures")
    draft = st.session_state.get("draft")

    if not draft:
        up = st.file_uploader("Dépose une facture : PDF, photo ou fichier Factur-X (.xml)",
                              type=["pdf", "png", "jpg", "jpeg", "webp", "xml"])
        c1, c2 = st.columns([1, 3])
        if c1.button("Lire la facture", type="primary", disabled=not up):
            with st.spinner("Lecture en cours…"):
                try:
                    st.session_state.draft = read_invoice(up.getvalue(), up.name)
                    st.session_state.draft["source_file"] = up.name
                    st.rerun()
                except ExtractionError as e:
                    st.error(str(e))
        if c2.button("ou saisir à la main"):
            st.session_state.draft = {"supplier": "", "invoice_number": "", "invoice_date": str(date.today()),
                                      "total_ht": None, "source_file": "",
                                      "lines": [{"label": "", "quantity": 1.0, "unit": "", "unit_price_ht": 0.0,
                                                 "is_product": True}]}
            st.rerun()
        st.caption("Les factures électroniques (Factur-X) sont lues directement, sans frais. "
                   + ("Les autres sont lues par l'IA." if api_key_available()
                      else "Pour les PDF scannés et les photos, il faut configurer la clé API Claude."))

        rows = conn.execute(
            "SELECT i.id, i.invoice_date, s.name AS supplier, i.invoice_number, i.total_ht FROM invoices i "
            "JOIN suppliers s ON s.id = i.supplier_id ORDER BY i.invoice_date DESC LIMIT 10").fetchall()
        if rows:
            st.subheader("Dernières factures")
            for r in rows:
                with st.container(border=True):
                    a, b, c = st.columns([2, 3, 2], vertical_alignment="center")
                    a.write(date.fromisoformat(r["invoice_date"]).strftime("%d/%m/%Y"))
                    b.write(f"**{r['supplier']}**, facture n° {r['invoice_number']}")
                    c.write(eur(r["total_ht"]) + " HT" if r["total_ht"] else "")
        return

    # Vérification d'une facture lue
    st.subheader("Vérifie la facture")
    if draft.get("method") == "facturx":
        st.success("Facture électronique lue directement : les montants sont exacts.")
    c1, c2, c3 = st.columns(3)
    supplier = c1.text_input("Fournisseur", draft["supplier"])
    number = c2.text_input("N° de facture", draft["invoice_number"])
    try:
        d0 = date.fromisoformat(draft["invoice_date"])
    except ValueError:
        d0 = date.today()
    inv_date = c3.date_input("Date", d0, format="DD/MM/YYYY")
    total = draft.get("total_ht") or 0.0

    ings = ingredients(conn)
    df = _prefill_lines(conn, supplier, draft["lines"])
    a, b = st.columns([4, 1], vertical_alignment="bottom")
    a.caption("✅ déjà connu, 🟡 à vérifier, 🔴 choisis le produit, ⚪ ignoré, ⚠ montant incohérent.  \n"
              "« kg/L/pièces par unité » : ce que contient une unité facturée (colis 6 × 1 L → 6).")
    with b:
        _new_ingredient_form(conn, "inv")
    edited = st.data_editor(
        df, num_rows="dynamic", width="stretch", hide_index=True,
        column_config={
            "": st.column_config.TextColumn(disabled=True, width=40),
            "Produit": st.column_config.SelectboxColumn(options=[IGNORE] + [i["name"] for i in ings], required=True),
            "kg/L/pièces par unité": st.column_config.NumberColumn(min_value=0.0, format="%.3f"),
            "Prix unitaire HT": st.column_config.NumberColumn(format="%.2f €"),
        },
        key=f"editor_{number}_{supplier}",
    )
    lines_sum = float((edited["Qté"].fillna(0) * edited["Prix unitaire HT"].fillna(0)).sum())
    if total and abs(lines_sum - total) > max(1.0, 0.01 * total):
        st.warning(f"Les lignes font {eur(lines_sum)} mais la facture indique {eur(total)} HT : "
                   "une ligne manque ou est mal lue.")

    b1, b2 = st.columns([1, 4])
    if b2.button("Annuler"):
        st.session_state.pop("draft", None)
        st.rerun()
    if b1.button("Valider", type="primary"):
        errors = []
        if not supplier.strip() or not number.strip():
            errors.append("Indique le fournisseur et le numéro de facture.")
        elif db.invoice_exists(conn, supplier, number):
            errors.append("Cette facture est déjà enregistrée.")
        id_by_name = {i["name"]: i["id"] for i in ings}
        lines = []
        for _, r in edited.iterrows():
            if not str(r["Désignation"] or "").strip():
                continue
            ing_id = id_by_name.get(r["Produit"]) if r["Produit"] and r["Produit"] != IGNORE else None
            bq = r["kg/L/pièces par unité"]
            bq = float(bq) if pd.notna(bq) and bq else None
            if ing_id and not bq:
                errors.append(f"« {r['Désignation']} » : indique combien de kg/L/pièces contient une unité.")
            q, pu = float(r["Qté"] or 0), float(r["Prix unitaire HT"] or 0)
            lines.append({"label": r["Désignation"], "quantity": q, "unit": r["Unité"] or "",
                          "unit_price_ht": pu, "line_total_ht": round(q * pu, 2),
                          "ingredient_id": ing_id, "base_qty_per_unit": bq if ing_id else None})
        if errors:
            for e in errors:
                st.error(e)
            return
        inv_id = db.save_invoice(conn, supplier, number, inv_date.isoformat(), lines, total or None,
                                 draft.get("source_file", ""))
        st.session_state.pop("draft", None)
        touched = {l["ingredient_id"] for l in lines if l["ingredient_id"]}
        line_ids = {r[0] for r in conn.execute("SELECT id FROM invoice_lines WHERE invoice_id = ?", (inv_id,))}
        hits = [a for a in actions.open_alerts(conn) if a.ingredient_id in touched]
        errs = [e for e in credits.billing_errors(conn, days=None) if e.line_id in line_ids]
        emailed = None
        to = db.get_text(conn, "alert_email")
        msg = notify.invoice_alert(conn, touched, line_ids)
        if msg and to and notify.email_configured():
            try:
                notify.send_email(to, *msg)
                emailed = to
            except Exception as e:  # l'enregistrement ne doit pas échouer à cause de l'e-mail
                emailed = f"échec : {e}"
        st.session_state["flash"] = {"alerts": [a.ingredient_id for a in hits],
                                     "errors": sum(e.amount for e in errs), "emailed": emailed}
        st.rerun()


def flash_after_invoice():
    flash = st.session_state.pop("flash", None)
    if flash is None:
        return
    conn = get_conn()
    st.success("Facture enregistrée.")
    for ing_id in flash["alerts"]:
        a = actions.alert_for(conn, ing_id)
        if a is None:
            continue
        with st.container(border=True):
            x, y = st.columns([5, 1], vertical_alignment="center")
            x.markdown(f"<span class='pill loss'>{pct(a.change, sign=True)}</span> **{a.name}** : "
                       f"<span class='red'>{eur(a.monthly_impact, 0)} de marge par mois</span>",
                       unsafe_allow_html=True)
            y.button("Traiter", key=f"flash_{ing_id}", on_click=go_product, args=(ing_id,), type="primary")
    if flash["errors"] > 0:
        st.warning(f"Prix au-dessus de tes tarifs négociés : {eur(flash['errors'])} HT d'avoir à demander "
                   "(voir **Alertes**).")
    if flash["emailed"]:
        st.caption(f"Alerte envoyée par e-mail à {flash['emailed']}.")


# --- Ma carte ------------------------------------------------------------------

def page_menu():
    conn = get_conn()
    st.title("Ma carte")
    reports = pricing.all_dish_reports(conn)
    target = db.get_setting(conn, "target_food_cost")
    dishes = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM dishes")}

    if reports:
        for r in sorted(reports, key=lambda r: r.margin_rate_now):
            with st.container(border=True):
                a, b, c, d = st.columns([3, 1, 2, 1], vertical_alignment="center")
                a.markdown(f"**{r.name}**  \n<span class='muted'>{eur(r.price_ttc)} TTC, "
                           f"{dishes[r.dish_id]['monthly_volume']:.0f} vendus par mois</span>", unsafe_allow_html=True)
                cls = "red" if r.food_cost_now > target else ""
                b.markdown(f"<span class='{cls}'><b>{pct(r.margin_rate_now, 0)}</b></span>  \n"
                           f"<span class='muted'>de marge</span>", unsafe_allow_html=True)
                if r.margin_drift_pts < -0.05:
                    c.markdown(f"<span class='red'>{r.margin_drift_pts:+.1f} pts</span>  \n".replace(".", ",")
                               + "<span class='muted'>depuis 90 jours</span>", unsafe_allow_html=True)
                if r.missing_prices:
                    c.markdown(f"<span class='muted'>Prix manquant : {', '.join(r.missing_prices)}</span>",
                               unsafe_allow_html=True)
                if d.button("Modifier", key=f"edit_{r.dish_id}", width="stretch"):
                    st.session_state["edit_dish"] = r.dish_id
        st.caption(f"En rouge : plus de {pct(target, 0)} de coût matière (réglable dans Paramètres).")
    else:
        st.info("Aucun plat. Ajoute ta première fiche technique ci-dessous.")

    if st.button("+ Ajouter un plat"):
        st.session_state["edit_dish"] = None
        st.session_state["new_dish"] = True
    editing = st.session_state.get("edit_dish")
    if editing is not None or st.session_state.get("new_dish"):
        dish_editor(conn, dishes.get(editing))


def dish_editor(conn, cur: dict | None):
    cur = cur or {"id": None, "name": "", "price_ttc": 0.0, "vat_rate": 0.10, "monthly_volume": 0.0}
    st.divider()
    st.subheader(cur["name"] or "Nouveau plat")
    c1, c2, c3 = st.columns([3, 1, 1])
    name = c1.text_input("Nom", cur["name"])
    price = c2.number_input("Prix TTC (€)", value=float(cur["price_ttc"]), min_value=0.0, step=0.5)
    volume = c3.number_input("Vendus / mois", value=float(cur["monthly_volume"]), min_value=0.0, step=10.0)

    ings = ingredients(conn)
    unit_of = {i["name"]: i["base_unit"] for i in ings}
    items = []
    if cur["id"]:
        items = [dict(r) for r in conn.execute(
            "SELECT i.name AS Produit, di.quantity AS Quantité, di.yield_pct AS Rendement "
            "FROM dish_items di JOIN ingredients i ON i.id = di.ingredient_id WHERE di.dish_id = ?", (cur["id"],))]
    df = pd.DataFrame(items or [{"Produit": None, "Quantité": 0.0, "Rendement": 1.0}])
    df.insert(2, "Unité", df["Produit"].map(unit_of))
    st.caption("Quantité dans l'assiette, en kg, L ou pièces (180 g → 0,18). "
               "Rendement : 0,8 si 20 % de perte à l'épluchage ou à la cuisson.")
    edited = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch", key=f"dish_{cur['id']}",
                            column_config={
                                "Produit": st.column_config.SelectboxColumn(options=list(unit_of), required=True),
                                "Quantité": st.column_config.NumberColumn(min_value=0.0, format="%.3f"),
                                "Unité": st.column_config.TextColumn(disabled=True, width=60),
                                "Rendement": st.column_config.NumberColumn(min_value=0.05, max_value=1.0, format="%.2f"),
                            })
    with st.expander("TVA"):
        rates = [0.10, 0.055, 0.20]
        vat_rate = st.radio("Taux", rates, index=rates.index(cur["vat_rate"]) if cur["vat_rate"] in rates else 0,
                            format_func=lambda v: pct(v, 1 if v == 0.055 else 0), horizontal=True,
                            label_visibility="collapsed")

    b1, b2, b3 = st.columns([1, 1, 3])
    if b1.button("Enregistrer", type="primary"):
        if not name.strip() or price <= 0:
            st.error("Indique un nom et un prix.")
            return
        id_by_name = {i["name"]: i["id"] for i in ings}
        with db.transaction(conn):
            if cur["id"]:
                conn.execute("UPDATE dishes SET name=?, price_ttc=?, vat_rate=?, monthly_volume=? WHERE id=?",
                             (name.strip(), price, vat_rate, volume, cur["id"]))
                did = cur["id"]
                conn.execute("DELETE FROM dish_items WHERE dish_id = ?", (did,))
            else:
                did = conn.execute("INSERT INTO dishes(name, price_ttc, vat_rate, monthly_volume) VALUES (?, ?, ?, ?)",
                                   (name.strip(), price, vat_rate, volume)).lastrowid
            for _, r in edited.iterrows():
                if r["Produit"] in id_by_name and r["Quantité"] and r["Quantité"] > 0:
                    conn.execute("INSERT INTO dish_items(dish_id, ingredient_id, quantity, yield_pct) VALUES (?, ?, ?, ?)",
                                 (did, id_by_name[r["Produit"]], float(r["Quantité"]), float(r["Rendement"] or 1.0)))
        st.session_state.pop("edit_dish", None)
        st.session_state.pop("new_dish", None)
        st.rerun()
    if b2.button("Fermer"):
        st.session_state.pop("edit_dish", None)
        st.session_state.pop("new_dish", None)
        st.rerun()
    if cur["id"] and b3.button("Supprimer ce plat"):
        conn.execute("DELETE FROM dishes WHERE id = ?", (cur["id"],))
        conn.commit()
        st.session_state.pop("edit_dish", None)
        st.rerun()


# --- Paramètres ----------------------------------------------------------------

def page_settings():
    conn = get_conn()
    st.title("Paramètres")
    with st.form("settings"):
        name = st.text_input("Nom du restaurant (signature des e-mails)", db.get_text(conn, "restaurant_name"))
        email = st.text_input("Recevoir les alertes à cette adresse", db.get_text(conn, "alert_email"),
                              placeholder="toi@exemple.fr")
        thr = st.slider("Alerter à partir d'une hausse de", 1, 30,
                        int(round(db.get_setting(conn, "alert_threshold") * 100)), format="%d %%")
        eur_min = st.slider("…ou dès qu'une hausse coûte au moins (par mois)", 10, 300,
                            int(db.get_setting(conn, "alert_min_monthly")), step=10, format="%d €")
        days = st.slider("Comparer au prix moyen des derniers", 30, 365,
                         int(db.get_setting(conn, "reference_days")), format="%d jours")
        tgt = st.slider("Coût matière maximum par plat", 15, 50,
                        int(round(db.get_setting(conn, "target_food_cost") * 100)), format="%d %%")
        if st.form_submit_button("Enregistrer", type="primary"):
            db.set_setting(conn, "restaurant_name", name.strip())
            db.set_setting(conn, "alert_email", email.strip())
            db.set_setting(conn, "alert_threshold", thr / 100)
            db.set_setting(conn, "alert_min_monthly", eur_min)
            db.set_setting(conn, "reference_days", days)
            db.set_setting(conn, "target_food_cost", tgt / 100)
            st.success("C'est enregistré.")

    st.subheader("Alertes par e-mail")
    to = db.get_text(conn, "alert_email")
    if notify.email_configured():
        st.write("Envoi activé : une alerte part à chaque facture qui contient une hausse ou une erreur.")
        c1, c2 = st.columns(2)
        if c1.button("Envoyer le résumé de la semaine", disabled=not to, width="stretch"):
            try:
                notify.send_email(to, *notify.weekly_summary(conn))
                st.success(f"Résumé envoyé à {to}.")
            except Exception as e:
                st.error(f"L'envoi a échoué : {e}")
        if not to:
            c2.caption("Indique ton adresse ci-dessus.")
    else:
        st.write("Envoi non configuré. Ajoute `SMTP_HOST`, `SMTP_USER` et `SMTP_PASSWORD` dans les secrets "
                 "de l'app (voir le README).")
    with st.expander("Aperçu du résumé de la semaine"):
        subject, body = notify.weekly_summary(conn)
        st.code(f"Objet : {subject}\n\n{body}", language=None, wrap_lines=True)

    st.subheader("Démo")
    c1, c2 = st.columns(2)
    if c1.button("Charger la brasserie de démonstration", type="primary"):
        try:
            demo.load_demo(conn)
            st.session_state.pop("produit", None)
            st.switch_page(PAGES["alertes"])
        except RuntimeError as e:
            st.error(str(e))
    with c2.popover("Tout effacer"):
        st.write("Supprime toutes les factures, produits, plats et alertes traitées.")
        if st.button("Oui, tout effacer"):
            demo.reset(conn)
            st.session_state.pop("produit", None)
            st.rerun()
    st.caption("Lecture des factures scannées par l'IA : " + ("active ✅" if api_key_available()
                                                               else "inactive (clé API absente)"))


def main():
    load_env()
    st.set_page_config(page_title="Marge Resto", page_icon="🍽️", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    st.logo(str(ROOT / "assets" / "logo.svg"), size="large", icon_image=str(ROOT / "assets" / "logo-icon.svg"))
    n = 0
    try:
        n = len(actions.open_alerts(get_conn()))
    except Exception:
        pass
    PAGES.update({
        "alertes": st.Page(page_alerts, title=f"Alertes ({n})" if n else "Alertes", icon="🔔",
                           url_path="alertes", default=True),
        "produit": st.Page(page_product, title="Par produit", icon="🔎", url_path="produit"),
        "factures": st.Page(page_invoices, title="Factures", icon="🧾", url_path="factures"),
    })
    secondary = {
        "carte": st.Page(page_menu, title="Ma carte", icon="🍽️", url_path="carte"),
        "parametres": st.Page(page_settings, title="Paramètres", icon="⚙️", url_path="parametres"),
    }
    PAGES.update(secondary)
    nav = st.navigation({"": [PAGES["alertes"], PAGES["produit"], PAGES["factures"]],
                         "Réglages": list(secondary.values())})
    flash_after_invoice()
    nav.run()


if __name__ == "__main__":
    main()
