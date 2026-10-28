#!/bin/sh
# TypeLogic AI launcher for Linux and macOS: ./typelogic.sh for the menu, or ./typelogic.sh start --mode cpu|gpu|hybrid,
# setup, models, verify, cli ..., settings, diagnose, test, build-native, stack. See README.md.
set -eu
cd "$(dirname "$0")"
for candidate in .venv/bin/python python3.12 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then exec "$candidate" launcher/launcher.py "$@"; fi
done
echo "Python 3.10+ was not found." >&2
exit 1
