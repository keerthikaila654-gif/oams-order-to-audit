#!/usr/bin/env sh
# One command: install deps (if needed), create + seed the database, start the app on http://localhost:5000
set -e
python -m pip install -q -r requirements.txt
python manage.py run
