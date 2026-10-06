#!/usr/bin/env sh
set -e
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python run.py
