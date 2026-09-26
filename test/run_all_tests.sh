#!/usr/bin/env bash
set -euo pipefail;

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)";

bash "${PROJECT_ROOT}/test/generate_fixtures.sh";
bash "${PROJECT_ROOT}/test/test_collector.sh";
python3 "${PROJECT_ROOT}/test/test_analyzer.py";

printf 'All Evidence Tool tests passed.\n';
