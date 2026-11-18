#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .local
uv run pytest -q -n 8
uv run lint-imports
uv run python scripts/check_boundaries.py
uv run python scripts/check_ports.py
uv run python scripts/check_knowledge.py
printf '全部检查通过\n'
