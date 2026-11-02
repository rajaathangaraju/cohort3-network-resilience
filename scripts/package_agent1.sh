#!/usr/bin/env bash
set -euo pipefail

# Build Agent 1 (Weather & Network Intelligence Agent) direct-code CodeZip artifact
# for Amazon Bedrock AgentCore Runtime (PYTHON_3_13, Linux ARM64 / aarch64).
# Cross-packages dependencies even when the EC2 build host is x86_64.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DIST_DIR="$PROJECT_ROOT/dist"
STAGE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/c3_agent1_stage.XXXXXX")"
REQ_FILE="$(mktemp "${TMPDIR:-/tmp}/c3_agent1_reqs.XXXXXX.txt")"
ZIP_PATH="$DIST_DIR/agent1_weather_network_intelligence.zip"

PYTHON_VERSION="${AGENTCORE_PYTHON_VERSION:-3.13}"
UV_PLATFORM="${AGENTCORE_UV_PLATFORM:-aarch64-manylinux2014}"
PIP_PLATFORM="${AGENTCORE_PIP_PLATFORM:-manylinux2014_aarch64}"
ENTRYPOINT_REL="agents/weather_network_intelligence/agentcore_app.py"
SKIP_PIP_INSTALL="${SKIP_PIP_INSTALL:-0}"

trap 'rm -rf "$STAGE_DIR" "$REQ_FILE"' EXIT

rm -f "$ZIP_PATH"
mkdir -p "$DIST_DIR"

echo "============================================================"
echo "Packaging Agent 1 for Amazon Bedrock AgentCore Runtime"
echo "Runtime: PYTHON_3_13 | Target Architecture: Linux ARM64 (aarch64)"
echo "Entrypoint: $ENTRYPOINT_REL"
echo "============================================================"

# 1. Extract runtime dependencies directly from pyproject.toml (excluding dev dependencies)
python3 - "$PROJECT_ROOT/pyproject.toml" "$REQ_FILE" <<'PYREQ'
from pathlib import Path
import sys
import tomllib

pyproject_path = Path(sys.argv[1])
req_out = Path(sys.argv[2])
data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
deps = data.get("project", {}).get("dependencies", [])
if not deps:
    raise SystemExit("ERROR: No runtime dependencies found in pyproject.toml")
req_out.write_text("\n".join(deps) + "\n", encoding="utf-8")
print("Runtime dependencies extracted from pyproject.toml:")
for d in deps:
    print(f"  - {d}")
PYREQ

# 2. Copy minimal Agent 1 source (excluding Agent 2, orchestration, governance, tests, docs, etc.)
mkdir -p "$STAGE_DIR/agents/weather_network_intelligence"
cp "$PROJECT_ROOT/agents/__init__.py" "$STAGE_DIR/agents/__init__.py"
for mod in __init__.py agent.py agentcore_app.py inventory_service.py main.py network_mapper.py prompts.py weather_service.py; do
    cp "$PROJECT_ROOT/agents/weather_network_intelligence/$mod" "$STAGE_DIR/agents/weather_network_intelligence/$mod"
done

mkdir -p "$STAGE_DIR/common"
for mod in __init__.py config.py constants.py exceptions.py logging.py tracing.py; do
    cp "$PROJECT_ROOT/common/$mod" "$STAGE_DIR/common/$mod"
done

mkdir -p "$STAGE_DIR/contracts"
for mod in __init__.py action_result.py base.py governance_decision.py network_risk_event.py recommendation.py; do
    cp "$PROJECT_ROOT/contracts/$mod" "$STAGE_DIR/contracts/$mod"
done

mkdir -p "$STAGE_DIR/integrations"
for mod in __init__.py agentcore.py cloudwatch.py inventory_api.py weather_api.py; do
    cp "$PROJECT_ROOT/integrations/$mod" "$STAGE_DIR/integrations/$mod"
done

# 3. Cross-package Linux ARM64 (aarch64) runtime dependencies
if [ "$SKIP_PIP_INSTALL" != "1" ]; then
    if command -v uv >/dev/null 2>&1; then
        echo "Installing ARM64 runtime dependencies via uv (--python-platform $UV_PLATFORM --python-version $PYTHON_VERSION) ..."
        uv pip install \
            --system-certs \
            --python-platform "$UV_PLATFORM" \
            --python-version "$PYTHON_VERSION" \
            --target "$STAGE_DIR" \
            --only-binary=:all: \
            -r "$REQ_FILE"
    else
        echo "Installing ARM64 runtime dependencies via pip (--platform $PIP_PLATFORM --python-version $PYTHON_VERSION) ..."
        python3 -m pip install \
            --target "$STAGE_DIR" \
            --platform "$PIP_PLATFORM" \
            --implementation cp \
            --python-version "$PYTHON_VERSION" \
            --only-binary=:all: \
            --no-compile \
            --upgrade \
            -r "$REQ_FILE"
    fi
fi

# 4. Remove build/cache/secret artifacts before validation and ZIP creation
find "$STAGE_DIR" \( -name "__pycache__" -o -name ".pytest_cache" -o -name ".mypy_cache" -o -name ".ruff_cache" \) -type d -prune -exec rm -rf {} +
find "$STAGE_DIR" \( -name "*.pyc" -o -name "*.pyo" -o -name "*.pem" -o -name ".env" \) -type f -delete

# 5. Validate entrypoint inside staged package
PACKAGED_ENTRYPOINT="$STAGE_DIR/$ENTRYPOINT_REL"
if [ ! -f "$PACKAGED_ENTRYPOINT" ]; then
    echo "ERROR: Packaged entrypoint is missing: $PACKAGED_ENTRYPOINT" >&2
    exit 1
fi

if ! grep -q "from agents.weather_network_intelligence.agent import WeatherNetworkIntelligenceAgent" "$PACKAGED_ENTRYPOINT"; then
    echo "ERROR: Packaged entrypoint does not contain required absolute import for WeatherNetworkIntelligenceAgent" >&2
    exit 1
fi

if grep -q "from \.agent import WeatherNetworkIntelligenceAgent" "$PACKAGED_ENTRYPOINT"; then
    echo "ERROR: Packaged entrypoint still contains forbidden relative import 'from .agent import WeatherNetworkIntelligenceAgent'" >&2
    exit 1
fi
echo "ENTRYPOINT_IMPORT_VALIDATION=PASSED ($ENTRYPOINT_REL)"

# 6. Validate ARM64 shared libraries (*.so) and fail immediately if any x86/x86_64 binary is present
if command -v file >/dev/null 2>&1; then
    echo "Inspecting native shared libraries (*.so) with 'file' ..."
    SO_REPORT="$(find "$STAGE_DIR" -type f -name "*.so" -exec file {} \;)"
    if [ -n "$SO_REPORT" ]; then
        echo "$SO_REPORT"
        if echo "$SO_REPORT" | grep -E -i "x86-64|x86_64|80386"; then
            echo "ERROR: x86/x86_64 shared library detected in ARM64 package!" >&2
            exit 1
        fi
    fi
fi

python3 - "$STAGE_DIR" <<'PYELF'
import os
from pathlib import Path
import struct
import sys

stage = Path(sys.argv[1]).resolve()
so_files = sorted(stage.rglob("*.so"))
if not so_files:
    print("WARNING: No .so native shared libraries found in stage directory.")
    sys.exit(0)

EM_AARCH64 = 183
EM_X86_64 = 62
EM_386 = 3

detected = []
x86_found = []

for so_path in so_files:
    rel = so_path.relative_to(stage).as_posix()
    with so_path.open("rb") as f:
        header = f.read(20)
    if len(header) < 20 or header[:4] != b"\x7fELF":
        raise SystemExit(f"ERROR: Non-ELF binary encountered: {rel}")
    e_machine = struct.unpack("<H", header[18:20])[0]
    if e_machine == EM_AARCH64:
        arch_label = "ELF 64-bit LSB shared object, ARM aarch64"
    elif e_machine in (EM_X86_64, EM_386):
        arch_label = f"ELF x86/x86_64 (e_machine={e_machine})"
        x86_found.append((rel, arch_label))
    else:
        arch_label = f"ELF unknown architecture (e_machine={e_machine})"
        x86_found.append((rel, arch_label))
    detected.append((rel, arch_label))

print(f"Detected {len(detected)} native shared library file(s):")
for rel, arch_label in detected:
    print(f"  {rel}: {arch_label}")

if x86_found:
    raise SystemExit(f"ERROR: Non-ARM64 binaries detected: {x86_found}")

print("ARM64_BINARY_VALIDATION=PASSED (0 x86/x86_64 binaries found)")
PYELF

# 7. Create dist/agent1_weather_network_intelligence.zip
python3 - "$STAGE_DIR" "$ZIP_PATH" "$ENTRYPOINT_REL" <<'PYZIP'
import os
from pathlib import Path
import sys
import zipfile

stage = Path(sys.argv[1]).resolve()
zip_out = Path(sys.argv[2]).resolve()
entrypoint_rel = sys.argv[3]

with zipfile.ZipFile(zip_out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(stage):
        dirs[:] = sorted(
            d for d in dirs
            if d not in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")
        )
        for file_name in sorted(files):
            if file_name.endswith((".pyc", ".pyo", ".pem", ".env")):
                continue
            full_path = Path(root) / file_name
            rel_path = full_path.relative_to(stage).as_posix()
            zf.write(full_path, rel_path)

size_bytes = zip_out.stat().st_size
with zipfile.ZipFile(zip_out, "r") as zf:
    names = zf.namelist()
    assert entrypoint_rel in names, f"Missing {entrypoint_rel} in zip archive!"

print("============================================================")
print(f"ZIP_LOCATION={zip_out}")
print(f"ZIP_SIZE_BYTES={size_bytes}")
print(f"ZIP_SIZE_MB={size_bytes / (1024 * 1024):.2f}")
print("PYTHON_RUNTIME=PYTHON_3_13")
print("TARGET_ARCHITECTURE=ARM64 (aarch64-manylinux2014)")
print(f"ENTRYPOINT={entrypoint_rel}")
print(f"PACKAGED_FILE_COUNT={len(names)}")
print("============================================================")
PYZIP
