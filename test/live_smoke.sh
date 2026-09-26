#!/usr/bin/env bash
# Read-only live check against a real systemd host.
#
# The rest of the suite runs against synthetic fixtures, which proves the
# collector's contract but says nothing about a real manager. This script
# closes that gap: it collects from a unit you name, verifies the bundle
# against its own manifest, and runs the analyzer over the result.
#
# It changes no service state. It reads. Run it against any unit you can
# already inspect, including one that is running normally.

set -euo pipefail;

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)";
COLLECTOR_SCRIPT="${PROJECT_ROOT}/collect_systemd_evidence.sh";
ANALYZER_SCRIPT="${PROJECT_ROOT}/analyze_evidence_bundle.py";

fail() {
  printf 'LIVE SMOKE FAILURE: %s\n' "${1}" >&2;
  exit 1;
}

UNIT_NAME="${1:-}";
[[ -n "${UNIT_NAME}" ]] || {
  printf 'Usage: bash test/live_smoke.sh NAME.service\n' >&2;
  printf 'Example: bash test/live_smoke.sh ssh.service\n' >&2;
  exit 2;
};

command -v systemctl > /dev/null || fail "systemctl is not on PATH";
[[ -d /run/systemd/system ]] || fail "systemd is not running as PID 1 on this host";

printf '== Host ==\n';
systemd-analyze --version 2>/dev/null | head -n 1 || printf 'systemd-analyze unavailable\n';
printf 'euid: %s\n' "$(id -u)";
printf 'groups: %s\n' "$(id -nG)";

OUTPUT_DIRECTORY="$(mktemp -d)";
printf '\n== Collect ==\n';
bash "${COLLECTOR_SCRIPT}" --unit "${UNIT_NAME}" --output-dir "${OUTPUT_DIRECTORY}" --include-unit-verify;

BUNDLE_DIRECTORY="$(find "${OUTPUT_DIRECTORY}" -maxdepth 1 -mindepth 1 -type d -name 'evidence-*' | head -n 1)";
[[ -n "${BUNDLE_DIRECTORY}" ]] || fail "no bundle directory was created";

printf '\n== Bundle integrity ==\n';
( cd "${BUNDLE_DIRECTORY}" && awk -F'\t' 'NR>1 {printf "%s  %s\n", $2, $1}' manifest.tsv | sha256sum -c - ) \
  || fail "manifest digests do not match the files in the bundle";
( cd "${BUNDLE_DIRECTORY}" && sha256sum -c manifest.sha256 ) || fail "manifest.sha256 does not match manifest.tsv";

printf '\n== Collection metadata ==\n';
grep -E '"(mode|collection_status|journal_access|schema_version)"' "${BUNDLE_DIRECTORY}/collection.json";

printf '\n== Outcomes ==\n';
cat "${BUNDLE_DIRECTORY}/command-outcomes.tsv";

printf '\n== Analyze ==\n';
REPORT_PARENT_DIRECTORY="$(mktemp -d)";
python3 "${ANALYZER_SCRIPT}" --bundle "${BUNDLE_DIRECTORY}" --output-dir "${REPORT_PARENT_DIRECTORY}" > /dev/null;
REPORT_DIRECTORY="$(find "${REPORT_PARENT_DIRECTORY}" -maxdepth 1 -mindepth 1 -type d -name 'report-*' | head -n 1)";
[[ -n "${REPORT_DIRECTORY}" ]] || fail "no report directory was created";
sed -n '1,40p' "${REPORT_DIRECTORY}/analysis.md";

printf '\nBundle: %s\n' "${BUNDLE_DIRECTORY}";
printf 'Report: %s\n' "${REPORT_DIRECTORY}";
printf 'Live smoke completed. Both directories are private to you; delete them when you are done.\n';
