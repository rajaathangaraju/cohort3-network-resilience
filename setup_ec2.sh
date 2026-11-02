#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$HOME/cohort3-network-resilience"
cd "$PROJECT_DIR"

echo "Working directory: $(pwd)"
python3 --version

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment .venv ..."
    python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "Upgrading pip, setuptools, wheel, and hatchling ..."
python -m pip install --upgrade pip setuptools wheel hatchling

echo "Installing project and dev dependencies from pyproject.toml ..."
python -m pip install -e ".[dev]"

cat <<'VALIDATION_EOF'

============================================================
Setup complete. Run the following validation commands:
============================================================

python -m ruff check .
python -m mypy .
python -m pytest -v

python -m orchestration.workflow --scenario deny
python -m orchestration.workflow --scenario allow
python -m orchestration.workflow --scenario pause
python -m orchestration.workflow --scenario pause --approve --approver demo-approver

============================================================
Live Amazon Bedrock smoke test commands:
============================================================

export AWS_REGION=us-east-2
export BEDROCK_ENABLED=true
export RUN_LIVE_BEDROCK_SMOKE=1
export BEDROCK_MODEL_ID="<VALID_MODEL_ID>"

aws sts get-caller-identity

python -m pytest tests/integration/test_bedrock_live_smoke.py -v -s
VALIDATION_EOF
