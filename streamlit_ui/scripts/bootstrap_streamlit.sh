#!/usr/bin/env bash
set -euo pipefail

# Prepare the repository virtual environment for the Streamlit Command Center UI.
# Creates <repo>/.venv only when missing, installs the Streamlit dependencies, and
# creates runtime/log directories. Does not start Streamlit.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STREAM_UI_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$STREAM_UI_DIR/.." && pwd)"

VENV_DIR="${VENV_DIR:-$REPO_ROOT/.venv}"
REQUIREMENTS_FILE="$STREAM_UI_DIR/requirements.txt"
STREAMLIT_ENTRYPOINT="$STREAM_UI_DIR/dashboard.py"
RUNTIME_DIR="$STREAM_UI_DIR/runtime"
LOG_DIR="$STREAM_UI_DIR/logs"

trap 'printf "BOOTSTRAP_STREAMLIT=FAILED (line %s)\n" "$LINENO" >&2' ERR

fail() {
    trap - ERR
    printf 'ERROR: %s\n' "$*" >&2
    printf 'BOOTSTRAP_STREAMLIT=FAILED\n'
    exit 1
}

python_is_313() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 13) else 1)' >/dev/null 2>&1
}

find_base_python() {
    local candidate
    for candidate in "${PYTHON:-}" python3.13 python3 python; do
        if [[ -n "$candidate" ]] && command -v "$candidate" >/dev/null 2>&1 && python_is_313 "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

resolve_venv_bin() {
    if [[ -x "$VENV_DIR/bin/python" ]]; then
        VENV_BIN="$VENV_DIR/bin"
    elif [[ -x "$VENV_DIR/Scripts/python.exe" ]]; then
        VENV_BIN="$VENV_DIR/Scripts"
    else
        VENV_BIN="$VENV_DIR/bin"
    fi
    PYTHON_BIN="$VENV_BIN/python"
    STREAMLIT_BIN="$VENV_BIN/streamlit"
}

[[ -f "$REQUIREMENTS_FILE" ]] || fail "Streamlit requirements not found at $REQUIREMENTS_FILE."
[[ -f "$STREAMLIT_ENTRYPOINT" ]] || fail "Streamlit entrypoint not found at $STREAMLIT_ENTRYPOINT."

resolve_venv_bin
if [[ -x "$PYTHON_BIN" ]]; then
    echo "Reusing existing virtual environment: $VENV_DIR"
else
    BASE_PYTHON="$(find_base_python)" || fail "Python 3.13+ was not found on PATH (tried \$PYTHON, python3.13, python3, python)."
    echo "Creating virtual environment $VENV_DIR with $BASE_PYTHON"
    "$BASE_PYTHON" -m venv "$VENV_DIR"
    resolve_venv_bin
fi

PYTHON_VERSION="$("$PYTHON_BIN" -c 'import platform; print(platform.python_version())')"
python_is_313 "$PYTHON_BIN" || fail "$VENV_DIR uses Python $PYTHON_VERSION; Python 3.13+ is required. The existing environment was not modified."
echo "PYTHON_VERSION=$PYTHON_VERSION"

if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    echo "pip is missing from $VENV_DIR; bootstrapping it with ensurepip."
    "$PYTHON_BIN" -m ensurepip --upgrade
fi

echo "Installing Streamlit dependencies from $REQUIREMENTS_FILE"
"$PYTHON_BIN" -m pip install --disable-pip-version-check --quiet -r "$REQUIREMENTS_FILE"
if ! "$PYTHON_BIN" -c 'import pytest' >/dev/null 2>&1; then
    # build_streamlit.sh runs the Streamlit unit tests.
    echo "Installing pytest for build_streamlit.sh"
    "$PYTHON_BIN" -m pip install --disable-pip-version-check --quiet "pytest>=8.3.0"
fi

mkdir -p "$RUNTIME_DIR" "$LOG_DIR"
echo "RUNTIME_DIR=$RUNTIME_DIR"
echo "LOG_DIR=$LOG_DIR"

[[ -x "$STREAMLIT_BIN" ]] || fail "Streamlit executable not found at $STREAMLIT_BIN after installation."
MIN_STREAMLIT_VERSION="$(sed -n 's/^streamlit>=\([0-9.]*\).*/\1/p' "$REQUIREMENTS_FILE" | head -n 1)"
STREAMLIT_VERSION="$("$PYTHON_BIN" - "${MIN_STREAMLIT_VERSION:-0}" <<'PY'
import sys

import streamlit

def as_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".")[:3] if part.isdigit())

if as_tuple(streamlit.__version__) < as_tuple(sys.argv[1]):
    sys.exit(f"Streamlit {streamlit.__version__} is older than the required {sys.argv[1]}")
print(streamlit.__version__)
PY
)" || fail "Streamlit version check failed."
echo "STREAMLIT_VERSION=$STREAMLIT_VERSION"

JSON_VIEW_BACKUP="$RUNTIME_DIR/dashboard.py.pre-json-view-removal.bak"
JSON_VIEW_RESULT="$("$PYTHON_BIN" - "$STREAMLIT_ENTRYPOINT" "$JSON_VIEW_BACKUP" <<'PY'
import re
import shutil
import sys

path, backup = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8", newline="") as handle:
    original = handle.read()
newline = "\r\n" if "\r\n" in original else "\n"
text = original.replace("\r\n", "\n")

text = re.sub(
    r'^[ \t]*with st\.expander\("Sanitized raw evidence JSON"\):\n[ \t]+st\.json\([^\n]*\)\n',
    "",
    text,
    flags=re.M,
)
text = re.sub(
    r'^[ \t]*if view\.get\("ai_reasoning"\):\n[ \t]+st\.json\([^\n]*\)\n',
    "",
    text,
    flags=re.M,
)
text = re.sub(
    r'^with st\.expander\("Evidence and raw API response"\):\n'
    r"[ \t]+if evidence_items:\n"
    r"[ \t]+(st\.dataframe\([^\n]*\))\n"
    r'[ \t]+if view\.get\("raw_payload"\):\n'
    r"[ \t]+st\.json\([^\n]*\)\n",
    'if evidence_items:\n    with st.expander("Evidence"):\n        \\1\n',
    text,
    flags=re.M,
)
if "redact_secrets(" not in text:
    text = re.sub(r"^[ \t]+redact_secrets,\n", "", text, flags=re.M)

text = text.replace("\n", newline)
if text == original:
    print("UNCHANGED")
else:
    shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    print("PATCHED")
PY
)" || fail "Could not remove the raw JSON views from $STREAMLIT_ENTRYPOINT."
if [[ "$JSON_VIEW_RESULT" == "PATCHED" ]]; then
    echo "Removed raw JSON views from $STREAMLIT_ENTRYPOINT (backup: $JSON_VIEW_BACKUP)"
fi

if ! "$PYTHON_BIN" -m py_compile "$STREAMLIT_ENTRYPOINT"; then
    if [[ "$JSON_VIEW_RESULT" == "PATCHED" ]]; then
        cp -p "$JSON_VIEW_BACKUP" "$STREAMLIT_ENTRYPOINT"
        fail "Entrypoint did not compile after removing the JSON views; restored the original from $JSON_VIEW_BACKUP."
    fi
    fail "Entrypoint $STREAMLIT_ENTRYPOINT does not compile."
fi
echo "STREAMLIT_ENTRYPOINT=$STREAMLIT_ENTRYPOINT"

# Raw JSON views were removed from the UI; refuse to deploy a dashboard that still renders them.
if json_views="$(grep -nE '\bst\.json\(' "$STREAMLIT_ENTRYPOINT")"; then
    printf '%s\n' "$json_views" | cut -d: -f1 | sed 's/^/  dashboard.py line /' >&2
    fail "$STREAMLIT_ENTRYPOINT still renders raw JSON views (st.json). Update to the latest dashboard.py."
fi
echo "DASHBOARD_JSON_VIEWS=REMOVED"

echo "BOOTSTRAP_STREAMLIT=PASSED"
