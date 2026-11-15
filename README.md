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

## Le problème résolu

Les prix fournisseurs montent par petites touches, les prix de la carte ne bougent pas, et la marge fond sans qu'on le voie jusqu'au bilan. Marge Resto détecte chaque hausse dès la facture, la chiffre sur tous les plats concernés et propose quoi faire.

## Comment ça marche

| Page | Ce qu'on y fait |
| --- | --- |
| **Alertes** | La marge à récupérer chaque mois, les hausses à traiter (fortes en % **ou** chères en €), les avoirs à demander quand un prix dépasse le tarif négocié (e-mail prêt à envoyer), et la marge déjà récupérée. |
| **Par produit** | Pour un produit en hausse : ce qu'elle coûte, et « Que faire ? » avec 4 options chiffrées : ajuster les prix de la carte (prix conseillés), changer de fournisseur, réduire la portion, absorber. Un clic applique l'option et l'alerte passe en « traitée ». |
| **Factures** | Dépôt d'une facture. Les factures électroniques Factur-X sont lues directement (gratuit, exact) ; les PDF scannés et photos par l'IA. À la validation, les hausses et erreurs apparaissent tout de suite, et partent par e-mail si c'est configuré. |
| Ma carte | Fiches techniques (grammages, rendement, prix, ventes par mois). |
| Paramètres | Seuils d'alerte, adresse e-mail, nom du restaurant, démo. |

Une hausse traitée ne revient pas : l'alerte suivante se calcule à partir du prix accepté. La marge « récupérée » est comptée au plus égale à la marge perdue.

## Alertes par e-mail

Ajoute dans les *Secrets* de l'app Streamlit (ou dans `.env`) :

```
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = "587"
SMTP_USER = "ton.adresse@gmail.com"
SMTP_PASSWORD = "mot de passe d'application"
```

Puis indique l'adresse qui reçoit les alertes dans **Paramètres**. Une alerte part à chaque facture contenant une hausse ou une erreur ; le résumé de la semaine s'envoie depuis Paramètres (pas d'envoi automatique tant que les données ne sont pas dans une base en ligne).

## Structure

```
app.py                     interface Streamlit (5 pages)
margeresto/db.py           base SQLite (data/marge.db) et enregistrement des factures
margeresto/extraction.py   lecture des factures via l'API Claude
margeresto/matching.py     rapprochement libellé → ingrédient, lecture des conditionnements (10X250G…)
margeresto/pricing.py      prix de référence, alertes, coût et marge par plat
margeresto/suppliers.py    comparateur de fournisseurs
margeresto/actions.py      alertes à traiter, options chiffrées, marge récupérée
margeresto/credits.py      tarifs négociés, écarts de facturation, e-mails d'avoir
margeresto/facturx.py      lecture des factures électroniques Factur-X
margeresto/notify.py       alertes et résumé par e-mail
margeresto/demo.py         brasserie de démonstration (3 fournisseurs, 5 mois, 5 plats)
tests/test_core.py         tests (python3 -m pytest)
```

## Limites du prototype

- Un seul restaurant, en local, sans comptes utilisateurs.
- La lecture des factures n'a pas encore été testée sur de vraies factures : c'est le premier test à faire.
- Pas d'import des ventes depuis la caisse : les portions vendues par mois se saisissent à la main.
- Données effacées à chaque mise à jour en ligne : il faut une base de données externe avant un vrai restaurant.
- Pas d'envoi automatique du résumé hebdomadaire, ni d'adresse e-mail pour transférer ses factures (nécessitent la base en ligne).
