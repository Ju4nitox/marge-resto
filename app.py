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


# --- Utilitaires ---------------------------------------------------------------

def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def eur(x: float, dec: int = 2) -> str:
    s = f"{x:,.{dec}f}".replace(",", " ").replace(".", ",")
    return f"{s} €"


def pct(x: float, dec: int = 1, sign: bool = False) -> str:
    s = f"{x * 100:+.{dec}f}" if sign else f"{x * 100:.{dec}f}"
    return s.replace(".", ",") + " %"


@st.cache_resource
def get_conn():
    return db.connect(os.environ.get("MARGE_DB", db.DEFAULT_DB))


def ingredients(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM ingredients ORDER BY name")]


# --- Tableau de bord -----------------------------------------------------------

def page_dashboard():
    conn = get_conn()
    st.title("Tableau de bord")
    alerts = pricing.price_alerts(conn)
    reports = [r for r in pricing.all_dish_reports(conn) if r.cost_now > 0]
    target = db.get_setting(conn, "target_food_cost")

    if not reports and not alerts:
        st.info("Aucune donnée pour l'instant. Ajoute des factures et des plats, "
                "ou charge la brasserie de démonstration dans **Paramètres**.")
        return

    losing = [r for r in reports if r.margin_drift_pts < -0.05]
    impact = sum(r.monthly_impact for r in reports)
    above = [r for r in reports if r.food_cost_now > target]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Hausses de prix détectées", len(alerts))
    c2.metric("Plats dont la marge recule", len(losing))
    c3.metric("Marge perdue / mois", eur(-impact if impact < 0 else 0, 0),
              help="À volumes de vente constants, par rapport aux prix de référence.")
    c4.metric(f"Plats au-dessus de {pct(target, 0)} de ratio matière", len(above))

    if losing:
        st.subheader("Ce qui coûte le plus")
        for r in sorted(losing, key=lambda r: r.monthly_impact)[:5]:
            msg = (f"**{r.name}** : marge passée de {pct(r.margin_rate_ref)} à {pct(r.margin_rate_now)} "
                   f"({r.margin_drift_pts:+.1f} pts".replace(".", ","))
            if r.monthly_volume:
                msg += f", soit {eur(-r.monthly_impact, 0)} de marge en moins par mois"
            st.warning(msg + ").")

    st.subheader("Hausses de prix fournisseurs")
    if alerts:
        st.dataframe(pd.DataFrame([{
            "Ingrédient": a.name,
            "Prix de référence": f"{eur(a.reference)} / {a.base_unit}",
            "Dernier prix": f"{eur(a.current)} / {a.base_unit}",
            "Variation": pct(a.change, sign=True),
            "Fournisseur": a.supplier,
            "Date": a.current_date,
        } for a in alerts]), hide_index=True, width="stretch")
    else:
        st.success("Aucune hausse au-dessus du seuil d'alerte.")


# --- Factures -----------------------------------------------------------------

def _prefill_lines(conn, supplier: str, raw_lines: list[dict]) -> pd.DataFrame:
    ings = ingredients(conn)
    by_id = {i["id"]: i for i in ings}
    rows = []
    for ln in raw_lines:
        ing_name, bq, status = IGNORE, None, ""
        if not ln.get("is_product", True):
            status = "non-produit"
        else:
            known = db.known_mapping(conn, supplier, ln["label"]) if supplier else None
            if known:
                ing = by_id[known["ingredient_id"]]
                ing_name, bq, status = ing["name"], known["base_qty_per_unit"], "connu"
            else:
                sugg = suggest_ingredient(ln["label"], ings, limit=1)
                if sugg and sugg[0][1] >= 70:
                    ing = sugg[0][0]
                    ing_name = ing["name"]
                    bq = base_qty_from_unit(ln.get("unit", ""), ln["label"], ing["base_unit"])
                    status = "à vérifier"
                else:
                    status = "à compléter"
        if ln.get("warning"):
            status = f"{status} ⚠ {ln['warning']}".strip()
        rows.append({
            "Statut": status, "Désignation": ln["label"], "Qté": ln["quantity"], "Unité": ln.get("unit", ""),
            "PU HT": ln["unit_price_ht"], "Total HT": ln.get("line_total_ht"),
            "Ingrédient": ing_name, "Qté de base / unité": bq, "Réf.": ln.get("reference", ""),
        })
    return pd.DataFrame(rows)


def _new_ingredient_form(conn, key: str):
    with st.expander("Créer un ingrédient manquant"):
        with st.form(f"new_ing_{key}", clear_on_submit=True):
            c1, c2, c3 = st.columns([3, 1, 2])
            name = c1.text_input("Nom", placeholder="ex. Beurre doux")
            unit = c2.selectbox("Unité de base", db.BASE_UNITS)
            cat = c3.text_input("Catégorie (optionnel)")
            if st.form_submit_button("Créer") and name.strip():
                db.get_or_create_ingredient(conn, name, unit, cat)
                conn.commit()
                st.success(f"Ingrédient « {name} » créé.")
                st.rerun()


def page_invoices():
    conn = get_conn()
    st.title("Factures fournisseurs")
    tab_add, tab_list = st.tabs(["Ajouter une facture", "Factures enregistrées"])

    with tab_add:
        if not api_key_available():
            st.info("Lecture automatique désactivée : ajoute ta clé dans le fichier `.env` "
                    "(`ANTHROPIC_API_KEY=...`). Tu peux saisir la facture à la main en attendant.")
        up = st.file_uploader("Facture (PDF ou photo)", type=["pdf", "png", "jpg", "jpeg", "webp"])
        c1, c2 = st.columns(2)
        if c1.button("Lire la facture", type="primary", disabled=not (up and api_key_available())):
            with st.spinner("Lecture de la facture…"):
                try:
                    st.session_state.draft = extract_invoice(up.getvalue(), up.name)
                    st.session_state.draft["source_file"] = up.name
                except ExtractionError as e:
                    st.error(str(e))
        if c2.button("Saisie manuelle"):
            st.session_state.draft = {"supplier": "", "invoice_number": "", "invoice_date": str(date.today()),
                                      "total_ht": None, "source_file": "",
                                      "lines": [{"label": "", "quantity": 1.0, "unit": "", "unit_price_ht": 0.0,
                                                 "line_total_ht": None, "is_product": True}]}

        draft = st.session_state.get("draft")
        if not draft:
            return

        st.divider()
        st.subheader("Vérifier et valider")
        c1, c2, c3, c4 = st.columns(4)
        supplier = c1.text_input("Fournisseur", draft["supplier"])
        number = c2.text_input("N° de facture", draft["invoice_number"])
        try:
            d0 = date.fromisoformat(draft["invoice_date"])
        except ValueError:
            d0 = date.today()
        inv_date = c3.date_input("Date", d0, format="DD/MM/YYYY")
        total = c4.number_input("Total HT", value=float(draft["total_ht"] or 0.0), step=1.0)

        _new_ingredient_form(conn, "inv")
        ings = ingredients(conn)
        names = [IGNORE] + [i["name"] for i in ings]
        df = _prefill_lines(conn, supplier, draft["lines"])
        st.caption("Pour chaque ligne : choisis l'ingrédient et indique combien d'unités de base "
                   "(kg, L ou pièce) contient UNE unité facturée — ex. colis 6 × 1 L → 6. "
                   "Ce choix est mémorisé pour les prochaines factures de ce fournisseur.")
        edited = st.data_editor(
            df, num_rows="dynamic", width="stretch", hide_index=True,
            column_config={
                "Statut": st.column_config.TextColumn(disabled=True, width="small"),
                "Ingrédient": st.column_config.SelectboxColumn(options=names, required=True),
                "Qté de base / unité": st.column_config.NumberColumn(min_value=0.0, format="%.3f"),
                "PU HT": st.column_config.NumberColumn(format="%.2f €"),
                "Total HT": st.column_config.NumberColumn(format="%.2f €"),
            },
            key=f"editor_{number}_{supplier}",
        )
        lines_sum = float((edited["Qté"].fillna(0) * edited["PU HT"].fillna(0)).sum())
        if total and abs(lines_sum - total) > max(1.0, 0.01 * total):
            st.warning(f"Somme des lignes {eur(lines_sum)} ≠ total HT {eur(total)} : une ligne manque ou est mal lue.")

        if st.button("Valider la facture", type="primary"):
            errors = []
            if not supplier.strip() or not number.strip():
                errors.append("Fournisseur et numéro de facture obligatoires.")
            elif db.invoice_exists(conn, supplier, number):
                errors.append("Cette facture est déjà enregistrée.")
            id_by_name = {i["name"]: i["id"] for i in ings}
            lines = []
            for _, r in edited.iterrows():
                if not str(r["Désignation"] or "").strip():
                    continue
                ing = r["Ingrédient"]
                ing_id = id_by_name.get(ing) if ing and ing != IGNORE else None
                bq = r["Qté de base / unité"]
                bq = float(bq) if pd.notna(bq) and bq else None
                if ing_id and not bq:
                    errors.append(f"« {r['Désignation']} » : indique la quantité de base par unité.")
                lines.append({"label": r["Désignation"], "reference": r["Réf."] or "",
                              "quantity": float(r["Qté"] or 0), "unit": r["Unité"] or "",
                              "unit_price_ht": float(r["PU HT"] or 0),
                              "line_total_ht": float(r["Total HT"]) if pd.notna(r["Total HT"]) else None,
                              "ingredient_id": ing_id, "base_qty_per_unit": bq if ing_id else None})
            if errors:
                for e in errors:
                    st.error(e)
            else:
                db.save_invoice(conn, supplier, number, inv_date.isoformat(), lines,
                                total or None, draft.get("source_file", ""))
                st.session_state.pop("draft", None)
                touched = {l["ingredient_id"] for l in lines if l["ingredient_id"]}
                hits = [a for a in pricing.price_alerts(conn) if a.ingredient_id in touched]
                st.success("Facture enregistrée.")
                for a in hits:
                    st.warning(f"Hausse : **{a.name}** à {eur(a.current)} / {a.base_unit} "
                               f"({pct(a.change, sign=True)} vs référence).")

    with tab_list:
        rows = conn.execute(
            "SELECT i.id, i.invoice_date AS Date, s.name AS Fournisseur, i.invoice_number AS Numéro, "
            "i.total_ht AS 'Total HT', COUNT(l.id) AS Lignes FROM invoices i "
            "JOIN suppliers s ON s.id = i.supplier_id LEFT JOIN invoice_lines l ON l.invoice_id = i.id "
            "GROUP BY i.id ORDER BY i.invoice_date DESC").fetchall()
        if not rows:
            st.info("Aucune facture enregistrée.")
            return
        df = pd.DataFrame([dict(r) for r in rows])
        st.dataframe(df.drop(columns="id"), hide_index=True, width="stretch",
                     column_config={"Total HT": st.column_config.NumberColumn(format="%.2f €")})
        labels = {f"{r['Date']} · {r['Fournisseur']} · {r['Numéro']}": r["id"] for r in rows}
        pick = st.selectbox("Voir le détail", list(labels))
        det = conn.execute(
            "SELECT l.label AS Désignation, l.quantity AS Qté, l.unit AS Unité, l.unit_price_ht AS 'PU HT', "
            "COALESCE(g.name, '—') AS Ingrédient, l.base_qty_per_unit AS 'Qté de base / unité' "
            "FROM invoice_lines l LEFT JOIN ingredients g ON g.id = l.ingredient_id WHERE l.invoice_id = ?",
            (labels[pick],)).fetchall()
        st.dataframe(pd.DataFrame([dict(r) for r in det]), hide_index=True, width="stretch")
        if st.checkbox("Supprimer cette facture"):
            if st.button("Confirmer la suppression", type="secondary"):
                conn.execute("DELETE FROM invoices WHERE id = ?", (labels[pick],))
                conn.commit()
                st.rerun()


# --- Prix des ingrédients ------------------------------------------------------

def page_ingredients():
    conn = get_conn()
    st.title("Prix des ingrédients")
    _new_ingredient_form(conn, "ing")
    statuses = pricing.all_price_status(conn)
    if not statuses:
        st.info("Aucun ingrédient.")
        return
    st.dataframe(pd.DataFrame([{
        "Ingrédient": p.name, "Unité": p.base_unit,
        "Prix de référence": p.reference, "Dernier prix": p.current,
        "Variation": p.change * 100 if p.change is not None else None,
        "Dernier achat": p.current_date, "Fournisseur": p.supplier,
    } for p in statuses]), hide_index=True, width="stretch", column_config={
        "Prix de référence": st.column_config.NumberColumn(format="%.2f €"),
        "Dernier prix": st.column_config.NumberColumn(format="%.2f €"),
        "Variation": st.column_config.NumberColumn(format="%+.1f %%"),
    })
    by_name = {p.name: p for p in statuses}
    pick = st.selectbox("Historique des prix", list(by_name))
    hist = pricing.price_history(conn, by_name[pick].ingredient_id)
    if hist:
        h = pd.DataFrame(hist)
        h["date"] = pd.to_datetime(h["date"])
        st.line_chart(h.groupby("date")["price"].mean(), y_label=f"€ HT / {by_name[pick].base_unit}")
        st.dataframe(h.rename(columns={"date": "Date", "supplier": "Fournisseur", "label": "Libellé",
                                       "price": "Prix / unité de base"}),
                     hide_index=True, width="stretch",
                     column_config={"Prix / unité de base": st.column_config.NumberColumn(format="%.3f €")})


# --- Plats ---------------------------------------------------------------------

def page_dishes():
    conn = get_conn()
    st.title("Plats et marges")
    target = db.get_setting(conn, "target_food_cost")
    reports = pricing.all_dish_reports(conn)
    vat = {r["id"]: r["vat_rate"] for r in conn.execute("SELECT id, vat_rate FROM dishes")}
    if reports:
        st.dataframe(pd.DataFrame([{
            "Plat": r.name, "Prix TTC": r.price_ttc, "Coût matière": r.cost_now,
            "Ratio matière": r.food_cost_now * 100, "Marge": r.margin_rate_now * 100,
            "Évolution (pts)": r.margin_drift_pts, "Impact / mois": r.monthly_impact,
            "Prix TTC pour garder la marge en €": r.price_ttc_to_keep_margin_eur(vat[r.dish_id]),
            "Prix TTC pour garder la marge en %": r.price_ttc_to_keep_margin(vat[r.dish_id]),
            "Prix manquants": ", ".join(r.missing_prices),
        } for r in sorted(reports, key=lambda r: r.monthly_impact)]),
            hide_index=True, width="stretch", column_config={
                "Prix TTC": st.column_config.NumberColumn(format="%.2f €"),
                "Coût matière": st.column_config.NumberColumn(format="%.2f €"),
                "Ratio matière": st.column_config.NumberColumn(format="%.1f %%"),
                "Marge": st.column_config.NumberColumn(format="%.1f %%"),
                "Évolution (pts)": st.column_config.NumberColumn(format="%+.1f"),
                "Impact / mois": st.column_config.NumberColumn(format="%+.0f €"),
                "Prix TTC pour garder la marge en €": st.column_config.NumberColumn(format="%.2f €"),
                "Prix TTC pour garder la marge en %": st.column_config.NumberColumn(format="%.2f €"),
            })
        st.caption(f"Ratio matière cible : {pct(target, 0)}. « Évolution » compare au coût calculé avec "
                   "les prix de référence. Impact = écart de coût × portions vendues par mois.")

    st.subheader("Créer ou modifier une fiche technique")
    dishes = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM dishes ORDER BY name")}
    choice = st.selectbox("Plat", ["+ Nouveau plat"] + list(dishes))
    cur = dishes.get(choice, {"id": None, "name": "", "price_ttc": 0.0, "vat_rate": 0.10, "monthly_volume": 0.0})
    c1, c2, c3, c4 = st.columns([3, 1, 1, 1])
    name = c1.text_input("Nom du plat", cur["name"])
    price = c2.number_input("Prix TTC", value=float(cur["price_ttc"]), min_value=0.0, step=0.5)
    vat_rate = c3.selectbox("TVA", [0.10, 0.055, 0.20], index=[0.10, 0.055, 0.20].index(cur["vat_rate"])
                            if cur["vat_rate"] in (0.10, 0.055, 0.20) else 0,
                            format_func=lambda v: pct(v, 1 if v == 0.055 else 0))
    volume = c4.number_input("Portions / mois", value=float(cur["monthly_volume"]), min_value=0.0, step=10.0)

    ings = ingredients(conn)
    unit_of = {i["name"]: i["base_unit"] for i in ings}
    items = []
    if cur["id"]:
        items = [dict(r) for r in conn.execute(
            "SELECT i.name AS Ingrédient, di.quantity AS Quantité, di.yield_pct AS Rendement "
            "FROM dish_items di JOIN ingredients i ON i.id = di.ingredient_id WHERE di.dish_id = ?", (cur["id"],))]
    df = pd.DataFrame(items or [{"Ingrédient": None, "Quantité": 0.0, "Rendement": 1.0}])
    df["Unité"] = df["Ingrédient"].map(unit_of)
    st.caption("Quantité nette dans l'assiette, en unité de base de l'ingrédient (kg, L ou pièce). "
               "Rendement : 0,8 si 20 % de perte (épluchage, parage, cuisson).")
    edited = st.data_editor(df, num_rows="dynamic", hide_index=True, width="stretch",
                            key=f"dish_{choice}", column_config={
                                "Ingrédient": st.column_config.SelectboxColumn(options=list(unit_of), required=True),
                                "Quantité": st.column_config.NumberColumn(min_value=0.0, format="%.3f"),
                                "Rendement": st.column_config.NumberColumn(min_value=0.05, max_value=1.0, format="%.2f"),
                                "Unité": st.column_config.TextColumn(disabled=True),
                            })
    b1, b2 = st.columns([1, 5])
    if b1.button("Enregistrer", type="primary"):
        if not name.strip() or price <= 0:
            st.error("Nom et prix TTC obligatoires.")
        else:
            id_by_name = {i["name"]: i["id"] for i in ings}
            with db.transaction(conn):
                if cur["id"]:
                    conn.execute("UPDATE dishes SET name=?, price_ttc=?, vat_rate=?, monthly_volume=? WHERE id=?",
                                 (name.strip(), price, vat_rate, volume, cur["id"]))
                    did = cur["id"]
                    conn.execute("DELETE FROM dish_items WHERE dish_id = ?", (did,))
                else:
                    did = conn.execute("INSERT INTO dishes(name, price_ttc, vat_rate, monthly_volume) "
                                       "VALUES (?, ?, ?, ?)", (name.strip(), price, vat_rate, volume)).lastrowid
                for _, r in edited.iterrows():
                    if r["Ingrédient"] in id_by_name and r["Quantité"] and r["Quantité"] > 0:
                        conn.execute("INSERT INTO dish_items(dish_id, ingredient_id, quantity, yield_pct) "
                                     "VALUES (?, ?, ?, ?)",
                                     (did, id_by_name[r["Ingrédient"]], float(r["Quantité"]),
                                      float(r["Rendement"] or 1.0)))
            st.success("Fiche technique enregistrée.")
            st.rerun()
    if cur["id"] and b2.button("Supprimer ce plat"):
        conn.execute("DELETE FROM dishes WHERE id = ?", (cur["id"],))
        conn.commit()
        st.rerun()


# --- Paramètres ----------------------------------------------------------------

def page_settings():
    conn = get_conn()
    st.title("Paramètres")
    with st.form("settings"):
        thr = st.slider("Seuil d'alerte de hausse (%)", 1, 30, int(db.get_setting(conn, "alert_threshold") * 100))
        days = st.slider("Période de référence (jours)", 30, 365, int(db.get_setting(conn, "reference_days")))
        tgt = st.slider("Ratio matière cible (%)", 15, 50, int(db.get_setting(conn, "target_food_cost") * 100))
        if st.form_submit_button("Enregistrer"):
            db.set_setting(conn, "alert_threshold", thr / 100)
            db.set_setting(conn, "reference_days", days)
            db.set_setting(conn, "target_food_cost", tgt / 100)
            st.success("Paramètres enregistrés.")

    st.subheader("Lecture des factures")
    st.write("Clé API Claude : " + ("détectée ✅" if api_key_available() else "absente ❌ (fichier `.env`)"))

    st.subheader("Données")
    c1, c2 = st.columns(2)
    if c1.button("Charger la brasserie de démonstration"):
        try:
            demo.load_demo(conn)
            st.success("Démo chargée : 3 fournisseurs, 5 mois de factures, 5 plats.")
        except RuntimeError as e:
            st.error(str(e))
    confirm = c2.checkbox("Je veux tout effacer")
    if c2.button("Vider la base", disabled=not confirm):
        demo.reset(conn)
        st.success("Base vidée.")


def main():
    load_env()
    st.set_page_config(page_title="Marge Resto", page_icon="🍽️", layout="wide")
    nav = st.navigation([
        st.Page(page_dashboard, title="Tableau de bord", url_path="tableau", default=True),
        st.Page(page_invoices, title="Factures", url_path="factures"),
        st.Page(page_ingredients, title="Prix des ingrédients", url_path="prix"),
        st.Page(page_dishes, title="Plats et marges", url_path="plats"),
        st.Page(page_settings, title="Paramètres", url_path="parametres"),
    ])
    nav.run()


if __name__ == "__main__":
    main()
