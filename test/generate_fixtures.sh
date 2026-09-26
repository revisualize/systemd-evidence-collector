#!/usr/bin/env bash
# Regenerate the synthetic analyzer fixtures.
# Every bundle is produced by the collector's own --fixture mode, so the
# fixtures cannot drift from the collector's output format without this
# script being re-run and the difference showing up in review.
set -euo pipefail;

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)";
COLLECTOR_SCRIPT="${PROJECT_ROOT}/collect_systemd_evidence.sh";
TARGET="${PROJECT_ROOT}/test/fixtures/generated";

rm -rf "${TARGET}";
mkdir -p "${TARGET}";

for scenario in healthy failed_config permission_failure dependency_failure incomplete_bundle ignored_directive; do
  staging="$(mktemp -d)";
  bash "${COLLECTOR_SCRIPT}" \
    --unit "example-api.service" \
    --output-dir "${staging}" \
    --fixture "${scenario}" \
    --include-unit-verify > /dev/null;
  produced="$(find "${staging}" -maxdepth 1 -type d -name 'evidence-*' | head -1)";
  if [ -z "${produced}" ]; then
    printf 'fixture generation failed for scenario: %s\n' "${scenario}" >&2;
    exit 1;
  fi
  mv "${produced}" "${TARGET}/${scenario}";
  rm -rf "${staging}";
  printf 'generated: %s\n' "${scenario}";
done
