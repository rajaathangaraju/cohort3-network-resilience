$ErrorActionPreference = "Stop"

# Build Agent 1 (Weather & Network Intelligence Agent) direct-code CodeZip artifact
# for Amazon Bedrock AgentCore Runtime (PYTHON_3_13, Linux ARM64 / aarch64).
# Cross-packages dependencies even on Windows / x86_64 hosts.

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path (Join-Path $ScriptDir "..")).Path
$DistDir = Join-Path $ProjectRoot "dist"
$ZipPath = Join-Path $DistDir "agent1_weather_network_intelligence.zip"
$StageDir = Join-Path $env:TEMP "c3_agent1_stage"
$ReqFile = Join-Path $env:TEMP "c3_agent1_reqs.txt"
$UvPlatform = if ($env:AGENTCORE_UV_PLATFORM) { $env:AGENTCORE_UV_PLATFORM } else { "aarch64-manylinux2014" }
$PipPlatform = if ($env:AGENTCORE_PIP_PLATFORM) { $env:AGENTCORE_PIP_PLATFORM } else { "manylinux2014_aarch64" }
$PythonVersion = if ($env:AGENTCORE_PYTHON_VERSION) { $env:AGENTCORE_PYTHON_VERSION } else { "3.13" }
$EntrypointRel = "agents/weather_network_intelligence/agentcore_app.py"
$SkipPip = if ($env:SKIP_PIP_INSTALL) { $env:SKIP_PIP_INSTALL } else { "0" }

python -c "
import shutil
from pathlib import Path
for p in (Path(r'$StageDir'), Path(r'$DistDir\agent1_codezip_stage')):
    if p.exists():
        shutil.rmtree(r'\\?\\' + str(p.resolve()), ignore_errors=True)
"

if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
if (Test-Path $ReqFile) { Remove-Item -Force $ReqFile }
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null

# 1. Extract runtime dependencies from pyproject.toml
python -c "
from pathlib import Path
import tomllib

pyproject = Path(r'$ProjectRoot\pyproject.toml')
req_out = Path(r'$ReqFile')
data = tomllib.loads(pyproject.read_text(encoding='utf-8'))
deps = data.get('project', {}).get('dependencies', [])
if not deps:
    raise SystemExit('ERROR: No runtime dependencies in pyproject.toml')
req_out.write_text('\n'.join(deps) + '\n', encoding='utf-8')
print('Extracted runtime dependencies:', deps)
"

# 2. Copy minimal Agent 1 source
$Agent1Stage = Join-Path $StageDir "agents\weather_network_intelligence"
New-Item -ItemType Directory -Force -Path $Agent1Stage | Out-Null
Copy-Item (Join-Path $ProjectRoot "agents\__init__.py") (Join-Path $StageDir "agents\__init__.py")
foreach ($mod in @("__init__.py", "agent.py", "agentcore_app.py", "inventory_service.py", "main.py", "network_mapper.py", "prompts.py", "weather_service.py")) {
    Copy-Item (Join-Path $ProjectRoot "agents\weather_network_intelligence\$mod") (Join-Path $Agent1Stage $mod)
}

$CommonStage = Join-Path $StageDir "common"
New-Item -ItemType Directory -Force -Path $CommonStage | Out-Null
foreach ($mod in @("__init__.py", "config.py", "constants.py", "exceptions.py", "logging.py", "tracing.py")) {
    Copy-Item (Join-Path $ProjectRoot "common\$mod") (Join-Path $CommonStage $mod)
}

$ContractsStage = Join-Path $StageDir "contracts"
New-Item -ItemType Directory -Force -Path $ContractsStage | Out-Null
foreach ($mod in @("__init__.py", "action_result.py", "base.py", "governance_decision.py", "network_risk_event.py", "recommendation.py")) {
    Copy-Item (Join-Path $ProjectRoot "contracts\$mod") (Join-Path $ContractsStage $mod)
}

$IntegrationsStage = Join-Path $StageDir "integrations"
New-Item -ItemType Directory -Force -Path $IntegrationsStage | Out-Null
foreach ($mod in @("__init__.py", "agentcore.py", "cloudwatch.py", "inventory_api.py", "weather_api.py")) {
    Copy-Item (Join-Path $ProjectRoot "integrations\$mod") (Join-Path $IntegrationsStage $mod)
}

# 3. Cross-package Linux ARM64 (aarch64) runtime dependencies
if ($SkipPip -ne "1") {
    $uvCmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($uvCmd) {
        uv pip install `
            --system-certs `
            --python-platform $UvPlatform `
            --python-version $PythonVersion `
            --target $StageDir `
            --only-binary=:all: `
            -r $ReqFile
        if ($LASTEXITCODE -ne 0) { throw "uv pip install failed with exit code $LASTEXITCODE" }
    } else {
        python -m pip install `
            --target $StageDir `
            --platform $PipPlatform `
            --implementation cp `
            --python-version $PythonVersion `
            --only-binary=:all: `
            --no-compile `
            --upgrade `
            -r $ReqFile
        if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }
    }
}

# 4. Validate entrypoint, ARM64 ELF binaries, and build ZIP
python -c "
import os, shutil, struct, sys, zipfile
from pathlib import Path

stage = Path(r'$StageDir').resolve()
zip_out = Path(r'$ZipPath').resolve()
entrypoint_rel = '$EntrypointRel'

ep_file = stage / entrypoint_rel
if not ep_file.exists():
    raise SystemExit(f'ERROR: Missing packaged entrypoint {ep_file}')
ep_text = ep_file.read_text(encoding='utf-8')
if 'from agents.weather_network_intelligence.agent import WeatherNetworkIntelligenceAgent' not in ep_text:
    raise SystemExit('ERROR: Packaged entrypoint missing absolute import')
if 'from .agent import WeatherNetworkIntelligenceAgent' in ep_text:
    raise SystemExit('ERROR: Packaged entrypoint contains relative import')
print(f'ENTRYPOINT_IMPORT_VALIDATION=PASSED ({entrypoint_rel})')

EM_AARCH64 = 183
EM_X86_64 = 62
EM_386 = 3
so_files = sorted(stage.rglob('*.so'))
if not so_files:
    raise SystemExit('ERROR: No .so shared libraries found in stage directory')

for so_path in so_files:
    rel = so_path.relative_to(stage).as_posix()
    with so_path.open('rb') as f:
        hdr = f.read(20)
    if len(hdr) < 20 or hdr[:4] != b'\x7fELF':
        raise SystemExit(f'ERROR: Non-ELF binary found: {rel}')
    e_machine = struct.unpack('<H', hdr[18:20])[0]
    if e_machine != EM_AARCH64:
        raise SystemExit(f'ERROR: Non-ARM64 binary detected in {rel}: e_machine={e_machine}')
    print(f'  {rel}: ELF 64-bit LSB shared object, ARM aarch64 (e_machine={e_machine})')

print(f'ARM64_BINARY_VALIDATION=PASSED ({len(so_files)} ARM64 .so files, 0 x86_64 binaries)')

with zipfile.ZipFile(zip_out, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(stage):
        dirs[:] = sorted(d for d in dirs if d not in ('__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache'))
        for f in sorted(files):
            if f.endswith(('.pyc', '.pyo', '.pem', '.env')):
                continue
            fp = Path(root) / f
            rel = fp.relative_to(stage).as_posix()
            zf.write(fp, rel)

shutil.rmtree(r'\\?\\' + str(stage), ignore_errors=True)
size = zip_out.stat().st_size
with zipfile.ZipFile(zip_out, 'r') as zf:
    names = zf.namelist()
    assert entrypoint_rel in names

print('============================================================')
print(f'ZIP_LOCATION={zip_out}')
print(f'ZIP_SIZE_BYTES={size}')
print(f'ZIP_SIZE_MB={size / (1024 * 1024):.2f}')
print('PYTHON_RUNTIME=PYTHON_3_13')
print('TARGET_ARCHITECTURE=ARM64 (aarch64-manylinux2014)')
print(f'ENTRYPOINT={entrypoint_rel}')
print(f'PACKAGED_FILE_COUNT={len(names)}')
print('============================================================')
"
Remove-Item -Force $ReqFile -ErrorAction SilentlyContinue
