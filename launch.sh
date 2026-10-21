#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
	python -m venv .venv
fi
source .venv/Scripts/activate
pip install -q -r requirements.txt
python main.py
