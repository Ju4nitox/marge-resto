# Marge Resto — prototype

Suit les prix des fournisseurs d'un restaurant à partir de ses factures, et montre l'effet des hausses sur la marge de chaque plat : « ton burger est passé de 77,5 % à 76,2 % de marge, soit 194 € de moins par mois ».

## Lancer l'application (Mac)

1. Installe Python 3.11 ou plus récent si besoin (python.org).
2. Dézippe le dossier, puis double-clique sur `lancer.command`.
   La première fois, macOS peut bloquer le fichier : clic droit > Ouvrir. Si le fichier n'est pas exécutable, lance dans le Terminal : `chmod +x lancer.command`.
3. L'application s'ouvre dans le navigateur (http://localhost:8501).
4. Dans **Paramètres**, clique sur « Charger la brasserie de démonstration » pour voir l'outil avec des données.

Sans `lancer.command` :

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

## Activer la lecture automatique des factures

Copie `.env.example` en `.env` et colle ta clé API Anthropic (console.anthropic.com). Chaque facture coûte quelques centimes. Sans clé, tout fonctionne sauf le bouton « Lire la facture » : on saisit les lignes à la main.

## Comment ça marche

| Étape | Où | Ce qui se passe |
| --- | --- | --- |
| 1. Facture | Factures | PDF ou photo lu par Claude → lignes produit. Tu vérifies, tu associes chaque ligne à un ingrédient. |
| 2. Correspondance | Factures | Le lien « libellé fournisseur → ingrédient + conditionnement » est mémorisé : la facture suivante se remplit seule. |
| 3. Prix | Prix des ingrédients | Prix ramené au kg, au litre ou à la pièce. Prix de référence = moyenne des achats des 90 jours précédents. |
| 4. Fiches techniques | Plats et marges | Grammages nets, rendement, prix TTC, portions vendues par mois. |
| 5. Impact | Impact des hausses | Pour chaque matière première : marge perdue par mois sur tous les plats qui l'utilisent, part de la marge globale, simulation d'une hausse (ex. +10 % sur le bœuf). |
| 6. Alertes | Tableau de bord | Hausses au-dessus du seuil (5 % par défaut), plats dont la marge recule, € perdus par mois, prix conseillé. |

## Structure

```
app.py                     interface Streamlit (5 pages)
margeresto/db.py           base SQLite (data/marge.db) et enregistrement des factures
margeresto/extraction.py   lecture des factures via l'API Claude
margeresto/matching.py     rapprochement libellé → ingrédient, lecture des conditionnements (10X250G…)
margeresto/pricing.py      prix de référence, alertes, coût et marge par plat
margeresto/demo.py         brasserie de démonstration (3 fournisseurs, 5 mois, 5 plats)
tests/test_core.py         tests (python3 -m pytest)
```

## Limites du prototype

- Un seul restaurant, en local, sans comptes utilisateurs.
- La lecture des factures n'a pas encore été testée sur de vraies factures : c'est le premier test à faire.
- Pas d'import des ventes depuis la caisse : les portions vendues par mois se saisissent à la main.
- Pas de lecture des factures électroniques Factur-X (prochaine étape logique).
