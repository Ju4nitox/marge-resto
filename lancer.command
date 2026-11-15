#!/bin/bash
# Double-clic sur Mac pour lancer l'application (la 1re fois, installe les dépendances).
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
.venv/bin/streamlit run app.py
