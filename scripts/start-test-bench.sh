#!/usr/bin/env bash
# Start the single translator application with its Linux fake GasWorks/ProLab bench.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/../translator.py" --gui --start-test-bench
