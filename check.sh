#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
python3 -m unittest discover -s tests -v
python3 -m py_compile hub.py
python3 -m json.tool manifest.json > /dev/null
qmllint -I "${OMARCHY_PATH:-/usr/share/omarchy}/shell" BarWidget.qml Panel.qml
omarchy plugin validate .
git diff --check
