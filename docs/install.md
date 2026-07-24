# Install & setup

_[← Back to the README](../README.md)_

Prerequisites, platform setup, the Python interpreter note, and the pinned dependency baseline.

## Setup (Local Linux)

Requires Python 3.12+ and subscription-level Azure read access. Dependencies are
managed by `uv` from `pyproject.toml` and `uv.lock`.

Install OS prerequisites, clone the repo, install `uv`, and sync the locked
environment:

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl git

git clone git@github.com:cedric66/azure-python.git
cd azure-python

curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

uv python install 3.12
uv sync --frozen
mkdir -p reports exports
```

Configure `subscriptions.csv` with the subscriptions to scan, then authenticate
with one of the credential methods used by `DefaultAzureCredential`.

Azure CLI login:

```bash
az login
az account set --subscription "<subscription-id>"
```

Service-principal login:

```bash
export AZURE_CLIENT_ID="<app-id>"
export AZURE_CLIENT_SECRET="<secret>"
export AZURE_TENANT_ID="<tenant-id>"
```

Run the launcher through `uv`:

```bash
uv run python aks_report.py --help
uv run python aks_report.py list
uv run python aks_report.py inventory --all
uv run python aks_report.py cost --env dev
uv run python aks_report.py cost --all --commit-discount 0.35   # tune the reservation/SP discount assumption
```

Common `uv` commands for this project:

```bash
uv sync --frozen                         # install exactly from uv.lock
uv sync                                  # resync after pulling repo changes
uv run python aks_report.py ...          # run reports in the project env
uv run python tests/smoke_test.py        # run the offline smoke test
uv run python tests/test_spot_split.py   # run the spot-design fixture test
uv run python tests/test_spot_savings.py # run the spot-savings math test
uv add <package>                         # add a runtime dependency
uv lock                                  # refresh uv.lock after dependency edits
uv tree                                  # inspect resolved dependencies
```

## Setup (Local Windows)

```powershell
cd azure-python
py -3.12 -m pip install uv
uv sync
az login
uv run python aks_report.py --help
```

Requires Python 3.12+. Dependencies are managed by `uv` through
`pyproject.toml` and `uv.lock`; local commands can be run as
`uv run python aks_report.py ...`. Auth uses `DefaultAzureCredential`:
`az login` works, as do service principal env vars (`AZURE_CLIENT_ID`,
`AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`) or managed identity - no code
changes.

**Permissions:** `Reader` on each subscription. Cost reports additionally need
Cost Management read (included in Reader for most subscription types; if cost
queries return 401/403, ask for `Cost Management Reader`).

## Setup (Linux / Docker)

The intended runtime is Linux in Docker.

```bash
docker build -t aks-reporting .

## Python interpreter

The project pins Python 3.12 via `.python-version`, so `uv` fetches a managed
CPython 3.12 automatically. `requires-python` is `>=3.12`, so any 3.12+
interpreter is fine. On a box where the managed download fails (flaky network or
a restricted CDN — you'll see `Invalid tar file` / `stream error detected`) or
where only a newer Python (e.g. 3.13) is installed, point `uv` at the system
interpreter instead of downloading one:

```bash

## Dependency Baseline

`pyproject.toml` is the dependency source and `uv.lock` pins the resolved
environment for repeatable local and Docker runs on Python 3.12+:

| Package | Why it is used |
|---|---|
| `azure-identity` | Azure auth through service principal, managed identity, workload identity, or Azure CLI locally |
| `requests` | ARM, Resource Graph, Cost Management, Retail Prices REST calls |
| `pandas` | Cost and inventory aggregation before writing Excel |
| `openpyxl` | Multi-sheet XLSX generation, formatting, formulas, conditional formatting, native charts |
| `python-docx` | Markdown to DOCX export with configured styles |
| `reportlab` | Markdown to PDF export without Pandoc/LibreOffice in the Docker image |
| `PyYAML` | Human-editable style config files such as `examples/report_style.example.yaml` |
