"""Marge Resto — prototype Streamlit.

Lancer :  streamlit run app.py
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from margeresto import db, demo, pricing
from margeresto.extraction import ExtractionError, api_key_available, extract_invoice
from margeresto.matching import base_qty_from_unit, suggest_ingredient

ROOT = Path(__file__).resolve().parent
IGNORE = "— ignorer (frais, consigne…) —"
PAGES: dict = {}


# --- Mise en forme -------------------------------------------------------------

def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    try:  # secrets Streamlit Cloud
        if "ANTHROPIC_API_KEY" in st.secrets:
            os.environ.setdefault("ANTHROPIC_API_KEY", st.secrets["ANTHROPIC_API_KEY"])
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
        return f"{q:g} pièce" + ("s" if q > 1 else "")
    return f"{q:.2f} {unit}".replace(".", ",")


CSS = """
<style>
.block-container {max-width: 1100px; padding-top: 2rem;}
.big {font-size: 2.1rem; font-weight: 700; line-height: 1.1;}
.red {color: #c0392b;} .green {color: #1e8449;} .muted {color: #888; font-size: .9rem;}
.lead {font-size: 1.15rem; margin: .2rem 0 1.2rem 0;}
</style>
"""


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


# --- Accueil -------------------------------------------------------------------

def page_home():
    conn = get_conn()
    st.title("Ta marge en un coup d'œil")
    ranking = pricing.impact_ranking(conn)
    if not ranking:
        empty_state()
        return
    total_margin = pricing.monthly_margin(conn)
    lost = sum(r["monthly_impact"] for r in ranking)
    hikes = [r for r in ranking if r["monthly_impact"] < -0.5]
    n_alerts = len(pricing.price_alerts(conn))
    reports = [r for r in pricing.all_dish_reports(conn) if r.cost_now > 0]
    hit = [r for r in reports if r.margin_drift_pts < -0.05]

    color = "red" if lost < 0 else "green"
    st.markdown(f"<div class='lead'>Les hausses fournisseurs te coûtent "
                f"<span class='big {color}'>{eur(abs(lost), 0)}</span> de marge par mois</div>",
                unsafe_allow_html=True)
    c1, c2, c3 = st.columns(3)
    c1.metric("Sur un an", eur(lost * 12, 0))
    c2.metric("Produits en hausse", n_alerts,
              help=f"Hausse d'au moins {pct(db.get_setting(conn, 'alert_threshold'), 0)} (réglable dans Paramètres).")
    c3.metric("Plats touchés", f"{len(hit)} / {len(reports)}")

    st.subheader("Les hausses qui te coûtent le plus")
    if not hikes:
        st.success("Aucune hausse qui pèse sur ta marge en ce moment.")
    for r in hikes[:3]:
        with st.container(border=True):
            a, b, c = st.columns([3, 2, 1], vertical_alignment="center")
            a.markdown(f"**{r['name']}**  \n<span class='muted'>{r['supplier']} · "
                       f"{r['dishes']} plat{'s' if r['dishes'] > 1 else ''}</span>", unsafe_allow_html=True)
            b.markdown(f"<span class='red'><b>{pct(r['change'], sign=True)}</b> · "
                       f"{eur(r['monthly_impact'], 0)} / mois</span>", unsafe_allow_html=True)
            c.button("Voir", key=f"see_{r['ingredient_id']}", on_click=go_product,
                     args=(r["ingredient_id"],), width="stretch")

    if hit:
        st.subheader("Les plats à repricer")
        for r in sorted(hit, key=lambda r: r.monthly_impact)[:3]:
            vat = conn.execute("SELECT vat_rate FROM dishes WHERE id = ?", (r.dish_id,)).fetchone()[0]
            with st.container(border=True):
                a, b = st.columns([3, 3], vertical_alignment="center")
                a.markdown(f"**{r.name}**  \n<span class='muted'>marge {pct(r.margin_rate_ref)} → "
                           f"{pct(r.margin_rate_now)}</span>", unsafe_allow_html=True)
                b.markdown(f"Passer de **{eur(r.price_ttc)}** à **{eur(r.price_ttc_to_keep_margin_eur(vat))}**"
                           f"  \n<span class='muted'>pour garder la même marge par assiette</span>",
                           unsafe_allow_html=True)

    st.caption(f"Marge brute mensuelle de la carte : {eur(total_margin, 0)}. "
               "Calculs à volumes de vente constants, par rapport aux prix des 90 derniers jours.")


# --- Par produit ---------------------------------------------------------------

def page_product():
    conn = get_conn()
    st.title("Impact d'un produit")
    ranking = pricing.impact_ranking(conn)
    if not ranking:
        empty_state()
        return
    ids = [r["ingredient_id"] for r in ranking]
    label = {r["ingredient_id"]: f"{r['name']}  ({pct(r['change'], sign=True)})" if r["change"] is not None
             else r["name"] for r in ranking}
    default = st.session_state.get("produit", ids[0])
    pick = st.selectbox("Produit", ids, index=ids.index(default) if default in ids else 0,
                        format_func=label.get, label_visibility="collapsed")
    st.session_state["produit"] = pick

    sim = None
    with st.expander("Et si le prix bougeait ? (simulation)"):
        on = st.toggle("Simuler une variation à partir du prix actuel")
        if on:
            sim = st.slider("Variation du prix", -30, 50, 10, format="%d %%") / 100

    imp = pricing.ingredient_impact(conn, pick, sim)
    s, dishes = imp["status"], imp["dishes"]
    before, after = imp["price_before"], imp["price_after"]
    change = after / before - 1 if before else 0.0
    monthly = sum(d.monthly_impact for d in dishes)
    unit = s.base_unit

    # En-tête : 3 chiffres
    c1, c2, c3 = st.columns(3)
    c1.metric("Prix d'achat" + (" simulé" if sim is not None else ""), f"{eur(after)} / {unit}",
              pct(change, sign=True), delta_color="inverse")
    c2.metric("Impact sur ta marge", f"{eur(monthly, 0, sign=True)} / mois")
    c3.metric("Sur un an", eur(monthly * 12, 0, sign=True))
    st.caption(f"Avant : {eur(before)} / {unit}"
               + ("" if sim is not None else f" (moyenne des 90 jours précédents) · dernier achat chez {s.supplier}"))

    if not dishes:
        st.info("Ce produit n'entre dans aucun plat de la carte.")
        return

    # Un bloc par plat
    st.subheader(f"Dans tes plats ({len(dishes)})")
    for d in sorted(dishes, key=lambda d: d.monthly_impact):
        with st.container(border=True):
            a, b, c = st.columns([3, 2, 3], vertical_alignment="center")
            a.markdown(f"**{d.dish}**  \n<span class='muted'>{qty(d.gross_qty, unit)} par assiette · "
                       f"{d.monthly_volume:.0f} vendus / mois</span>", unsafe_allow_html=True)
            cls = "red" if d.monthly_impact < 0 else "green"
            b.markdown(f"<span class='{cls}'><b>{eur(d.monthly_impact, 0, sign=True)} / mois</b></span>  \n"
                       f"<span class='muted'>marge {pct(d.margin_rate_before)} → {pct(d.margin_rate_after)}</span>",
                       unsafe_allow_html=True)
            if abs(d.extra_per_portion) >= 0.005:
                c.markdown(f"Prix conseillé : **{eur(d.price_ttc_to_keep_margin_eur)}**  \n"
                           f"<span class='muted'>au lieu de {eur(d.price_ttc)}</span>", unsafe_allow_html=True)
            else:
                c.markdown("<span class='muted'>Rien à changer</span>", unsafe_allow_html=True)

    hist = pricing.price_history(conn, pick)
    if len(hist) > 1:
        with st.expander("Historique du prix"):
            h = pd.DataFrame(hist)
            h["date"] = pd.to_datetime(h["date"])
            st.line_chart(h.groupby("date")["price"].mean(), y_label=f"€ HT / {unit}", height=220)


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
        up = st.file_uploader("Dépose une facture (PDF ou photo)", type=["pdf", "png", "jpg", "jpeg", "webp"])
        c1, c2 = st.columns([1, 3])
        if c1.button("Lire la facture", type="primary", disabled=not (up and api_key_available())):
            with st.spinner("Lecture en cours…"):
                try:
                    st.session_state.draft = extract_invoice(up.getvalue(), up.name)
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
        if not api_key_available():
            st.caption("Lecture automatique inactive : la clé API Claude n'est pas configurée.")

        rows = conn.execute(
            "SELECT i.id, i.invoice_date, s.name AS supplier, i.invoice_number, i.total_ht FROM invoices i "
            "JOIN suppliers s ON s.id = i.supplier_id ORDER BY i.invoice_date DESC LIMIT 10").fetchall()
        if rows:
            st.subheader("Dernières factures")
            for r in rows:
                with st.container(border=True):
                    a, b, c = st.columns([2, 3, 2], vertical_alignment="center")
                    a.write(date.fromisoformat(r["invoice_date"]).strftime("%d/%m/%Y"))
                    b.write(f"**{r['supplier']}** · n° {r['invoice_number']}")
                    c.write(eur(r["total_ht"]) + " HT" if r["total_ht"] else "")
        return

    # Vérification d'une facture lue
    st.subheader("Vérifie la facture")
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
    a.caption("✅ déjà connu · 🟡 à vérifier · 🔴 choisis le produit · ⚪ ignoré · ⚠ montant incohérent.  \n"
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
        db.save_invoice(conn, supplier, number, inv_date.isoformat(), lines, total or None,
                        draft.get("source_file", ""))
        st.session_state.pop("draft", None)
        touched = {l["ingredient_id"] for l in lines if l["ingredient_id"]}
        st.session_state["flash"] = [a for a in pricing.price_alerts(conn) if a.ingredient_id in touched]
        st.rerun()


def flash_after_invoice():
    alerts = st.session_state.pop("flash", None)
    if alerts is None:
        return
    st.success("Facture enregistrée.")
    for a in alerts:
        st.warning(f"**{a.name}** en hausse de {pct(a.change, sign=True)} ({eur(a.current)} / {a.base_unit}).")


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
                a.markdown(f"**{r.name}**  \n<span class='muted'>{eur(r.price_ttc)} TTC · "
                           f"{dishes[r.dish_id]['monthly_volume']:.0f} / mois</span>", unsafe_allow_html=True)
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
        thr = st.slider("Alerter à partir d'une hausse de", 1, 30,
                        int(round(db.get_setting(conn, "alert_threshold") * 100)), format="%d %%")
        days = st.slider("Comparer au prix moyen des derniers", 30, 365,
                         int(db.get_setting(conn, "reference_days")), format="%d jours")
        tgt = st.slider("Coût matière maximum par plat", 15, 50,
                        int(round(db.get_setting(conn, "target_food_cost") * 100)), format="%d %%")
        if st.form_submit_button("Enregistrer"):
            db.set_setting(conn, "alert_threshold", thr / 100)
            db.set_setting(conn, "reference_days", days)
            db.set_setting(conn, "target_food_cost", tgt / 100)
            st.success("C'est enregistré.")

    st.subheader("Démo")
    c1, c2 = st.columns(2)
    if c1.button("Charger la brasserie de démonstration", type="primary"):
        try:
            demo.load_demo(conn)
            st.session_state.pop("produit", None)
            st.switch_page(PAGES["accueil"])
        except RuntimeError as e:
            st.error(str(e))
    with c2.popover("Tout effacer"):
        st.write("Supprime toutes les factures, produits et plats.")
        if st.button("Oui, tout effacer"):
            demo.reset(conn)
            st.session_state.pop("produit", None)
            st.rerun()
    st.caption("Lecture automatique des factures : " + ("active ✅" if api_key_available() else "inactive (clé API absente)"))


def main():
    load_env()
    st.set_page_config(page_title="Marge Resto", page_icon="🍽️", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    PAGES.update({
        "accueil": st.Page(page_home, title="Accueil", icon="🏠", url_path="accueil", default=True),
        "produit": st.Page(page_product, title="Par produit", icon="🔎", url_path="produit"),
        "factures": st.Page(page_invoices, title="Factures", icon="🧾", url_path="factures"),
        "carte": st.Page(page_menu, title="Ma carte", icon="🍽️", url_path="carte"),
        "parametres": st.Page(page_settings, title="Paramètres", icon="⚙️", url_path="parametres"),
    })
    nav = st.navigation(list(PAGES.values()))
    flash_after_invoice()
    nav.run()


if __name__ == "__main__":
    main()
