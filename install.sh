#!/usr/bin/env bash
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_bin=${PYTHON:-python3}

"$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else "arctl requires Python 3.11+")'
"$python_bin" -m venv "$source_dir/.venv"
"$source_dir/.venv/bin/python" -m pip install --editable "$source_dir"

echo "Installed arctl locally."
echo "Activate with: . $source_dir/.venv/bin/activate"
