#!/usr/bin/env bash
# One-command setup: creates .venv, installs deps + Playwright Chromium, creates folders.
set -euo pipefail
cd "$(dirname "$0")"

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "WARNING: ANTHROPIC_API_KEY is set in your shell. JobBot never uses it and strips it"
  echo "         from every claude subprocess, but you should unset it to be safe."
fi

PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r requirements.txt
pip install -e . >/dev/null
python -m playwright install chromium

mkdir -p data logs/reports/monthly screenshots resumes/variants resumes/tailored resumes/templates
[ -f .env ] || cp .env.example .env

command -v claude >/dev/null || echo "WARNING: 'claude' CLI not found on PATH. Install Claude Code and log in with your Pro account."

echo
echo "Setup complete. Activate with:  source .venv/bin/activate"
echo "Then try:                       jobbot discover"
