#!/usr/bin/env bash
# Deterministic safety and contract tests for the read-only Bash collector.

set -euo pipefail;

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)";
COLLECTOR_SCRIPT="${PROJECT_ROOT}/collect_systemd_evidence.sh";

fail() {
  printf 'TEST FAILURE: %s\n' "${1}" >&2;
  exit 1;
}

assert_file() {
  [[ -f "${1}" ]] || fail "expected file is missing: ${1}";
}

assert_mode() {
  local expected="${1}";
  local target="${2}";
  local actual;
  actual="$(stat -c '%a' "${target}")";
  [[ "${actual}" == "${expected}" ]] || fail "expected mode ${expected} for ${target}; found ${actual}";
}

assert_exit() {
  local expected="${1}";
  shift;
  set +e;
  "${@}" > "${TEST_STDOUT_FILE}" 2> "${TEST_STDERR_FILE}";
  local actual=$?;
  set -e;
  [[ "${actual}" -eq "${expected}" ]] || {
    cat "${TEST_STDOUT_FILE}" >&2 || true;
    cat "${TEST_STDERR_FILE}" >&2 || true;
    fail "expected exit ${expected} but got ${actual} for: ${*}";
  }
}

assert_file "${COLLECTOR_SCRIPT}";
bash -n "${COLLECTOR_SCRIPT}";

# Safety source scan: no systemctl state-changing command appears in executable code.
if grep -nE 'systemctl[[:space:]]+(start|stop|restart|reload|reset-failed|enable|disable|mask|unmask|kill)' "${COLLECTOR_SCRIPT}"; then
  fail "collector source contains a prohibited systemctl state-changing command";
fi
if grep -nE '^[[:space:]]*sudo[[:space:]]' "${COLLECTOR_SCRIPT}"; then
  fail "collector source invokes sudo";
fi
if grep -nE '(^|[^[:alnum:]_])eval([^[:alnum:]_]|$)' "${COLLECTOR_SCRIPT}"; then
  fail "collector source uses eval";
fi
if grep -nE 'curl[[:space:]].*(-L|--location)' "${COLLECTOR_SCRIPT}"; then
  fail "collector source follows redirects";
fi

TEMPORARY_ROOT="$(mktemp -d)";
trap 'rm -rf "${TEMPORARY_ROOT}"' EXIT;
# Per-run capture files: two concurrent suite runs must not overwrite each other.
TEST_STDOUT_FILE="${TEMPORARY_ROOT}/command.stdout";
TEST_STDERR_FILE="${TEMPORARY_ROOT}/command.stderr";

# Dry run must create no output artifacts.
DRY_PARENT="${TEMPORARY_ROOT}/dry-run";
assert_exit 0 bash "${COLLECTOR_SCRIPT}" --unit example-api.service --output-dir "${DRY_PARENT}" --fixture healthy --dry-run;
[[ ! -e "${DRY_PARENT}" ]] || fail "dry-run created output parent";

# Invalid unit and unsafe URL must be rejected before collection.
assert_exit 2 bash "${COLLECTOR_SCRIPT}" --unit '../unsafe.service' --output-dir "${TEMPORARY_ROOT}/invalid-unit" --fixture healthy;
assert_exit 2 bash "${COLLECTOR_SCRIPT}" --unit example-api.service --output-dir "${TEMPORARY_ROOT}/unsafe-url" --health-url 'http://example.com/healthz';
assert_exit 2 bash "${COLLECTOR_SCRIPT}" --unit example-api.service --output-dir "${TEMPORARY_ROOT}/credential-url" --health-url 'http://127.0.0.1:8088/healthz?token=bad';

# Fixture mode produces a private, complete, deterministic contract bundle.
OUTPUT_PARENT="${TEMPORARY_ROOT}/collector";
assert_exit 0 bash "${COLLECTOR_SCRIPT}" --unit example-api.service --output-dir "${OUTPUT_PARENT}" --fixture permission_failure;
BUNDLE_DIRECTORY="$(find "${OUTPUT_PARENT}" -maxdepth 1 -mindepth 1 -type d -name 'evidence-*' | head -n 1)";
[[ -n "${BUNDLE_DIRECTORY}" ]] || fail "fixture collector did not create bundle";
assert_mode 700 "${BUNDLE_DIRECTORY}";

for artifact in collection.json manifest.tsv command-outcomes.tsv collector-summary.md systemctl-status.txt systemctl-show.txt unit-effective.txt journal.txt process.txt health.txt health.stderr path-metadata.txt; do
  assert_file "${BUNDLE_DIRECTORY}/${artifact}";
  assert_mode 600 "${BUNDLE_DIRECTORY}/${artifact}";
done

python3 - "${BUNDLE_DIRECTORY}/collection.json" "${BUNDLE_DIRECTORY}/manifest.tsv" <<'PY'
import csv
import json
import pathlib
import sys

collection = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert collection["schema_version"] == "1.2"
assert collection["unit"] == "example-api.service"
assert collection["privacy"]["redaction_filter"] == "assignment_patterns_v2"
assert collection["collection_context"]["journal_access"] == "not_applicable_fixture"
assert collection["collection_context"]["systemd_version"] == "not_applicable_fixture"
assert collection["collection_status"] in {"complete", "complete_with_command_failures", "partial"}, collection["collection_status"]
# Fixture metadata must describe the operator's flags, never the scenario's props.
assert collection["inputs"]["health_url_supplied"] is False, collection["inputs"]
assert collection["inputs"]["path_supplied"] is False, collection["inputs"]

rows = list(csv.DictReader(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8").splitlines(), delimiter="\t"))
names = [row["artifact"] for row in rows]
assert "systemctl-show.txt" in names
assert "journal.txt" in names
# The manifest must cover the records that say how to read the evidence.
for record in ("command-outcomes.tsv", "collection.json", "collector-summary.md"):
    assert record in names, "manifest omits %s" % record
assert len(names) == len(set(names)), "manifest lists a file twice"

# artifact_count must mean the same thing to the collector and the analyzer.
bundle_dir = pathlib.Path(sys.argv[1]).parent
counted = sum(
    1
    for item in bundle_dir.iterdir()
    if item.is_file() and item.name not in {"manifest.tsv", "collection.json", "manifest.sha256"}
)
assert collection["artifact_count"] == counted, (collection["artifact_count"], counted)
PY

# The live property list must capture the restart policy the manager holds.
grep -qE -- '--property=([^,[:space:]]+,)*Restart(,|;|[[:space:]]|$)' "${COLLECTOR_SCRIPT}" \
  || fail "live systemctl show property list omits Restart";

# Unit verification keeps diagnostics even when the exit status is zero.
VERIFY_PARENT_DIRECTORY="${TEMPORARY_ROOT}/verify";
assert_exit 0 bash "${COLLECTOR_SCRIPT}" --unit example-api.service --output-dir "${VERIFY_PARENT_DIRECTORY}" --fixture ignored_directive --include-unit-verify;
VERIFY_BUNDLE_DIRECTORY="$(find "${VERIFY_PARENT_DIRECTORY}" -maxdepth 1 -mindepth 1 -type d -name 'evidence-*' | head -n 1)";
assert_file "${VERIFY_BUNDLE_DIRECTORY}/unit-verify.txt";
assert_mode 600 "${VERIFY_BUNDLE_DIRECTORY}/unit-verify.txt";
grep -q 'ignoring: on-failuer' "${VERIFY_BUNDLE_DIRECTORY}/unit-verify.txt" || fail "verify diagnostic missing from bundle";
[[ "$(grep -c '^unit-verify.txt' "${VERIFY_BUNDLE_DIRECTORY}/command-outcomes.tsv")" -eq 1 ]] || fail "unit-verify.txt must have exactly one outcome row";
[[ "$(grep -c '^unit-verify.txt' "${VERIFY_BUNDLE_DIRECTORY}/manifest.tsv")" -eq 1 ]] || fail "unit-verify.txt must have exactly one manifest row";
grep -q '^Restart=no$' "${VERIFY_BUNDLE_DIRECTORY}/systemctl-show.txt" || fail "ignored_directive fixture must report Restart=no";

# The safety boundary the README claims has to be a property of the source,
# not a sentence in a document. No state-changing verb may appear at all.
COLLECTOR_CODE_ONLY_FILE="${TEMPORARY_ROOT}/collector_code_only.sh";
sed -e '/^usage() {/,/^}/d' -e '/^[[:space:]]*#/d' "${COLLECTOR_SCRIPT}" > "${COLLECTOR_CODE_ONLY_FILE}";
grep -nE '(^|[^[:alnum:]_])sudo[[:space:]]' "${COLLECTOR_CODE_ONLY_FILE}" \
  && fail "collector code invokes sudo";
grep -nE 'systemctl([[:space:]]+-[^[:space:]]+)*[[:space:]]+(start|stop|restart|reload|enable|disable|mask|unmask|kill|reset-failed|edit|set-property|isolate|daemon-reload)([[:space:]]|$)' "${COLLECTOR_CODE_ONLY_FILE}" \
  && fail "collector code contains a state-changing systemctl invocation";
grep -nE '(^|[^[:alnum:]_])(rm|mv|truncate|tee)[[:space:]]+[^|;]*(/etc|/usr|/var|/run)/' "${COLLECTOR_CODE_ONLY_FILE}" \
  && fail "collector code writes outside its own output directory";

# A live run must never label its own artifacts as fixture output.
LIVE_PARENT_DIRECTORY="${TEMPORARY_ROOT}/live";
assert_exit 0 bash "${COLLECTOR_SCRIPT}" --unit nonexistent-for-tests.service --output-dir "${LIVE_PARENT_DIRECTORY}";
LIVE_BUNDLE_DIRECTORY="$(find "${LIVE_PARENT_DIRECTORY}" -maxdepth 1 -mindepth 1 -type d -name 'evidence-*' | head -n 1)";
grep -q 'synthetic fixture artifact' "${LIVE_BUNDLE_DIRECTORY}/command-outcomes.tsv" \
  && fail "a live bundle labelled an artifact as a synthetic fixture";
grep -q '"mode": "live_read_only"' "${LIVE_BUNDLE_DIRECTORY}/collection.json" || fail "live bundle does not record live mode";
grep -q '"journal_access"' "${LIVE_BUNDLE_DIRECTORY}/collection.json" || fail "live bundle records no journal access context";

# The manifest anchor must match the manifest it covers.
assert_file "${LIVE_BUNDLE_DIRECTORY}/manifest.sha256";
assert_mode 600 "${LIVE_BUNDLE_DIRECTORY}/manifest.sha256";
ANCHOR_DIGEST="$(awk '{print $1}' "${LIVE_BUNDLE_DIRECTORY}/manifest.sha256")";
ACTUAL_DIGEST="$(sha256sum "${LIVE_BUNDLE_DIRECTORY}/manifest.tsv" | awk '{print $1}')";
[[ "${ANCHOR_DIGEST}" == "${ACTUAL_DIGEST}" ]] || fail "manifest.sha256 does not match manifest.tsv";

# A bundle that lost artifacts must say so rather than report a clean run.
DEGRADED_PARENT_DIRECTORY="${TEMPORARY_ROOT}/degraded";
assert_exit 0 bash "${COLLECTOR_SCRIPT}" --unit nonexistent-for-tests.service --output-dir "${DEGRADED_PARENT_DIRECTORY}" --health-url 'http://127.0.0.1:9/nothing' --include-unit-verify;
DEGRADED_BUNDLE_DIRECTORY="$(find "${DEGRADED_PARENT_DIRECTORY}" -maxdepth 1 -mindepth 1 -type d -name 'evidence-*' | head -n 1)";
grep -qE '"collection_status": "(partial|complete_with_command_failures)"' "${DEGRADED_BUNDLE_DIRECTORY}/collection.json" \
  || fail "a degraded collection reported itself as complete";

# Redaction has to fail toward over-redaction. A partly redacted secret is the
# worst outcome: it looks handled and is not.
REDACTION_INPUT_FILE="${TEMPORARY_ROOT}/redact_in.txt";
REDACTION_OUTPUT_FILE="${TEMPORARY_ROOT}/redact_out.txt";
cat > "${REDACTION_INPUT_FILE}" <<'REDACT'
Environment="TOKEN=abc 123"
API_KEY = zzz111
Token=qqq
CLIENT_SECRET:shhh
DATABASE_URL=postgres://user:pw@host/db
ordinary line stays
REDACT
(
  # Consumed by the redact_to_file definition sourced below.
  # shellcheck disable=SC2034
  EVIDENCE_BUNDLE_DIRECTORY="${TEMPORARY_ROOT}";
  # shellcheck disable=SC1090
  eval "$(sed -n '/^redact_to_file() {/,/^}/p' "${COLLECTOR_SCRIPT}")";
  redact_to_file "${REDACTION_INPUT_FILE}" "${REDACTION_OUTPUT_FILE}";
);
for leaked in 'abc 123' 'zzz111' 'qqq' 'shhh' 'user:pw'; do
  grep -Fq -- "${leaked}" "${REDACTION_OUTPUT_FILE}" && fail "redaction left a secret value behind: ${leaked}";
done;
grep -Fq 'ordinary line stays' "${REDACTION_OUTPUT_FILE}" || fail "redaction removed an ordinary line";

# Two runs in the same second must not collide.
REPEAT_RUN_DIRECTORY="${TEMPORARY_ROOT}/twice";
assert_exit 0 bash "${COLLECTOR_SCRIPT}" --unit example-api.service --output-dir "${REPEAT_RUN_DIRECTORY}" --fixture healthy;
assert_exit 0 bash "${COLLECTOR_SCRIPT}" --unit example-api.service --output-dir "${REPEAT_RUN_DIRECTORY}" --fixture healthy;
[[ "$(find "${REPEAT_RUN_DIRECTORY}" -maxdepth 1 -mindepth 1 -type d -name 'evidence-*' | wc -l)" -eq 2 ]] \
  || fail "two runs in the same second did not produce two bundles";

# A missing required command is a usage error, never an artifact that reads
# like evidence about the unit.
EMPTY_PATH_DIRECTORY="${TEMPORARY_ROOT}/emptypath";
mkdir -p "${EMPTY_PATH_DIRECTORY}";
set +e;
BASH_EXECUTABLE="$(command -v bash)";
PATH="${EMPTY_PATH_DIRECTORY}" "${BASH_EXECUTABLE}" "${COLLECTOR_SCRIPT}" --unit example-api.service --output-dir "${TEMPORARY_ROOT}/nodeps" > /dev/null 2>&1;
DEPENDENCY_EXIT_CODE=$?;
set -e;
[[ "${DEPENDENCY_EXIT_CODE}" -eq 3 ]] || fail "collector did not exit 3 (environment error) when required commands were missing (got ${DEPENDENCY_EXIT_CODE})";

# A redaction that fails must produce an integrity error, never a captured
# artifact. The stub sed makes every transformation fail.
STUB_DIRECTORY="${TEMPORARY_ROOT}/stub_path";
mkdir -p "${STUB_DIRECTORY}";
printf '#!/bin/sh\nexit 1\n' > "${STUB_DIRECTORY}/sed";
chmod 755 "${STUB_DIRECTORY}/sed";
REDACTION_FAILURE_PARENT="${TEMPORARY_ROOT}/redaction_failure";
if command -v systemctl > /dev/null 2>&1; then
  PATH="${STUB_DIRECTORY}:${PATH}" bash "${COLLECTOR_SCRIPT}" \
    --unit nonexistent-for-tests.service --output-dir "${REDACTION_FAILURE_PARENT}" > /dev/null 2>&1 || true;
  REDACTION_FAILURE_BUNDLE="$(find "${REDACTION_FAILURE_PARENT}" -maxdepth 1 -mindepth 1 -type d -name 'evidence-*' | head -n 1)";
  [[ -n "${REDACTION_FAILURE_BUNDLE}" ]] || fail "no bundle was produced while testing redaction failure";
  grep -q 'redaction failed; artifact not retained' "${REDACTION_FAILURE_BUNDLE}/command-outcomes.tsv" \
    || fail "a failed redaction was not recorded as an integrity error";
  grep -q 'integrity_error' "${REDACTION_FAILURE_BUNDLE}/command-outcomes.tsv" \
    || fail "a failed redaction produced no integrity_error status";
  [[ ! -f "${REDACTION_FAILURE_BUNDLE}/systemctl-show.txt" ]] \
    || fail "an artifact whose redaction failed was retained anyway";
  grep -q '"collection_status": "partial"' "${REDACTION_FAILURE_BUNDLE}/collection.json" \
    || fail "a bundle that lost artifacts did not report itself as partial";
  grep -c '^systemctl-show.txt' "${REDACTION_FAILURE_BUNDLE}/manifest.tsv" > /dev/null \
    && fail "the manifest lists an artifact that was never retained";
else
  printf 'skipped: redaction-failure test needs systemctl on PATH\n';
fi;

# The journal window must carry an explicit zone. A bare timestamp is read in
# the host's local zone by systemd, which would make the recorded UTC value a lie.
grep -q "date -u +'%Y-%m-%d %H:%M:%S UTC'" "${COLLECTOR_SCRIPT}" \
  || fail "the journal until boundary does not carry an explicit UTC suffix";
grep -q '"journal_until_utc"' "${COLLECTOR_SCRIPT}" || fail "the collection window end is not recorded";
grep -q -- "--until=" "${COLLECTOR_SCRIPT}" || fail "the journal query is not bounded at both ends";

# The health probe must refuse a proxy, or a loopback URL can leave the host.
grep -q -- "--noproxy '\*'" "${COLLECTOR_SCRIPT}" || fail "the health probe does not disable proxies";

# An existing output parent keeps its own mode.
PRE_EXISTING_PARENT="${TEMPORARY_ROOT}/pre_existing";
mkdir -p "${PRE_EXISTING_PARENT}";
chmod 755 "${PRE_EXISTING_PARENT}";
assert_exit 0 bash "${COLLECTOR_SCRIPT}" --unit example-api.service --output-dir "${PRE_EXISTING_PARENT}" --fixture healthy;
PARENT_MODE="$(stat -c '%a' "${PRE_EXISTING_PARENT}")";
[[ "${PARENT_MODE}" == "755" ]] || fail "collector changed the mode of an existing output directory (now ${PARENT_MODE})";

# Metadata must describe hostname exposure honestly.
grep -q '"hostname_may_be_present": true' "${COLLECTOR_SCRIPT}" \
  || fail "collection metadata still claims hostnames are absent";

# The systemd version has to be the real one. A refactor once turned the option
# into --version_text, and the field silently became "unknown".
grep -q 'systemctl --version 2>/dev/null' "${COLLECTOR_SCRIPT}" \
  || fail "the collector does not call systemctl --version";
if command -v systemctl > /dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
  VERSION_PARENT="${TEMPORARY_ROOT}/version_capture";
  assert_exit 0 bash "${COLLECTOR_SCRIPT}" --unit nonexistent-for-tests.service --output-dir "${VERSION_PARENT}";
  VERSION_BUNDLE="$(find "${VERSION_PARENT}" -maxdepth 1 -mindepth 1 -type d -name 'evidence-*' | head -n 1)";
  grep -q '"systemd_version": "systemd ' "${VERSION_BUNDLE}/collection.json" \
    || fail "a live bundle did not record a systemd version";
else
  printf 'skipped: systemd version capture needs systemd as PID 1\n';
fi;

# No semantic token may carry a variable-naming suffix into a command or flag.
grep -nE -- '--[a-z-]+_(text|name|count|commands|value)' "${COLLECTOR_SCRIPT}" \
  && fail "a command-line option carries a variable-naming suffix";

# Collection records must be published through the checked writer.
for RECORD_NAME in collector-summary.md collection.json manifest.sha256; do
  RECORD_WRITE="$(printf 'write_private_text_file "%s%s}/%s"' '$' '{EVIDENCE_BUNDLE_DIRECTORY' "${RECORD_NAME}")";
  grep -Fq -- "${RECORD_WRITE}" "${COLLECTOR_SCRIPT}" \
    || fail "a collection record is written without the checked writer: ${RECORD_NAME}";
done;
grep -Fq 'fail_environment "cannot append to the command outcome record"' "${COLLECTOR_SCRIPT}" \
  || fail "a failed outcome append does not end the collection";

# The summary must describe health by its recorded status, not assume capture.
grep -Fq 'integrity error; the artifact was not retained' "${COLLECTOR_SCRIPT}" \
  || fail "the collector summary cannot report a health integrity error";

# A health failure must not stop the collector from attempting the rest.
grep -Fq 'health_temporary_file="";' "${COLLECTOR_SCRIPT}" \
  || fail "a health temporary-file failure still returns from live collection";

# Required records and vanished artifacts fail manifest finalization.
grep -Fq 'collection.json collector-summary.md; do' "${COLLECTOR_SCRIPT}" \
  || fail "the manifest writer no longer iterates the required collection records";
grep -Fq 'An artifact recorded as captured that is no longer on disk' "${COLLECTOR_SCRIPT}" \
  || fail "manifest finalization still skips a vanished captured artifact";

# The bundle directory mode is enforced, not assumed.
grep -Fq 'cannot secure the evidence bundle directory' "${COLLECTOR_SCRIPT}" \
  || fail "the bundle directory chmod result is unchecked";

# Fixture evidence is labelled as scenario-generated, never as operator input.
FIXTURE_EVIDENCE_PARENT="${TEMPORARY_ROOT}/fixture_evidence";
assert_exit 0 bash "${COLLECTOR_SCRIPT}" --unit example-api.service --output-dir "${FIXTURE_EVIDENCE_PARENT}" --fixture failed_config;
FIXTURE_EVIDENCE_BUNDLE="$(find "${FIXTURE_EVIDENCE_PARENT}" -maxdepth 1 -mindepth 1 -type d -name 'evidence-*' | head -n 1)";
grep -q '"health_probe_simulated": true' "${FIXTURE_EVIDENCE_BUNDLE}/collection.json" \
  || fail "a fixture with synthetic health evidence does not declare it";
grep -q '"health_url_supplied": false' "${FIXTURE_EVIDENCE_BUNDLE}/collection.json" \
  || fail "a fixture claims the operator supplied a health URL";

# curl reads ~/.curlrc and treats its lines as command-line options unless -q
# comes first. Without it, a file on the host can redirect the collector's own
# output outside the bundle. This was reproduced before it was fixed.
grep -q -- "curl -q --noproxy" "${COLLECTOR_SCRIPT}" \
  || fail "the health probe does not disable curl configuration files";

if command -v curl > /dev/null 2>&1 && command -v python3 > /dev/null 2>&1; then
  CURLRC_HOME="${TEMPORARY_ROOT}/curlrc_home";
  ESCAPE_TARGET="${TEMPORARY_ROOT}/escaped_by_curlrc.txt";
  mkdir -p "${CURLRC_HOME}";
  printf 'output = %s\n' "${ESCAPE_TARGET}" > "${CURLRC_HOME}/.curlrc";
  HEALTH_PORT=18099;
  python3 -m http.server "${HEALTH_PORT}" --bind 127.0.0.1 > /dev/null 2>&1 &
  HEALTH_SERVER_PID=$!;
  sleep 1;
  if kill -0 "${HEALTH_SERVER_PID}" 2>/dev/null; then
    CURLRC_PARENT="${TEMPORARY_ROOT}/curlrc_bundle";
    HOME="${CURLRC_HOME}" bash "${COLLECTOR_SCRIPT}" --unit nonexistent-for-tests.service \
      --output-dir "${CURLRC_PARENT}" --health-url "http://127.0.0.1:${HEALTH_PORT}/" > /dev/null 2>&1 || true;
    kill "${HEALTH_SERVER_PID}" 2>/dev/null || true;
    wait "${HEALTH_SERVER_PID}" 2>/dev/null || true;
    [[ ! -e "${ESCAPE_TARGET}" ]] \
      || fail "a .curlrc redirected collector output outside the bundle";
    CURLRC_BUNDLE="$(find "${CURLRC_PARENT}" -maxdepth 1 -mindepth 1 -type d -name 'evidence-*' | head -n 1)";
    assert_file "${CURLRC_BUNDLE}/health.txt";
  else
    printf 'skipped: curl configuration test needs a local listener\n';
  fi;
else
  printf 'skipped: curl configuration test needs curl and python3\n';
fi;

printf 'Collector tests passed.\n';
