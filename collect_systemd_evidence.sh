#!/usr/bin/env bash
# Evidence Tool collector v0.11.0
# Purpose: Create a private, read-only evidence bundle for one explicit systemd service.
# Safety boundary: this script performs no systemd service-state changes. It writes
# only inside its own output directory and invokes no sudo.

set -u;
set -o pipefail;
umask 077;

readonly COLLECTOR_VERSION="0.11.0";

UNIT_NAME="";
OUTPUT_PARENT_DIRECTORY="";
JOURNAL_SINCE="-30 min";
HEALTH_URL="";
TARGET_PATH="";
INCLUDE_UNIT_VERIFY=0;
DRY_RUN=0;
FIXTURE_SCENARIO="";

usage() {
  # printf is a builtin, so usage still prints when PATH is unusable.
  printf '%s\n' \
    'Usage:' \
    '  collect_systemd_evidence.sh --unit NAME.service --output-dir DIRECTORY [options]' \
    '' \
    'Required:' \
    '  --unit NAME.service       Explicit systemd service unit name.' \
    '  --output-dir DIRECTORY   Parent directory for the private evidence bundle.' \
    '' \
    'Optional:' \
    '  --since TIME              Literal journal time expression; default: -30 min.' \
    '  --health-url URL          Loopback-only HTTP URL, for example http://127.0.0.1:8088/healthz.' \
    '  --path PATH               Collect path metadata only; file contents are never read.' \
    '  --include-unit-verify     Run read-only systemd-analyze verify against discovered unit fragment.' \
    '  --fixture SCENARIO        Generate deterministic synthetic bundle: healthy, failed_config,' \
    '                            permission_failure, dependency_failure, incomplete_bundle,' \
    '                            or ignored_directive.' \
    '  --dry-run                 Print the plan and perform no collection.' \
    '  --help                    Show this help.' \
    '' \
    'Safety:' \
    '  This collector is observation-only. It invokes no sudo and changes no systemd or' \
    '  service state: no start, stop, restart, reload, reset, enable, disable, mask,' \
    '  unmask, kill, edit, copy, or delete of that state. It does write, move, and remove' \
    '  its own files inside the output directory you name.';
}

fail_environment() {
  printf 'Environment error: %s\n' "${1}" >&2;
  exit 3;
}

fail_usage() {
  printf 'Usage error: %s\n' "${1}" >&2;
  usage >&2;
  exit 2;
}

require_value() {
  local option="${1}";
  local value="${2:-}";
  [[ -n "${value}" ]] || fail_usage "${option} requires a value";
}

validate_unit() {
  local value="${1}";
  [[ "${value}" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]*\.service$ ]] || fail_usage "--unit must be a literal name ending in .service";
  [[ "${value}" != *".."* ]] || fail_usage "--unit must not contain consecutive dots";
  [[ "${value}" != -* ]] || fail_usage "--unit must not begin with a dash";
  [[ "${value}" != */* && "${value}" != *'*'* && "${value}" != *'?'* && "${value}" != *'['* && "${value}" != *']'* ]] || fail_usage "--unit must not contain paths or glob characters";
}

validate_since() {
  local value="${1}";
  [[ "${value}" =~ ^[A-Za-z0-9_:+[:space:]-]{1,64}$ ]] || fail_usage "--since contains unsupported characters";
}

validate_health_url() {
  local value="${1}";
  [[ "${value}" =~ ^http://127\.0\.0\.1:([0-9]{1,5})(/[^\?#[:space:]]*)?$ ]] || fail_usage "--health-url must be a loopback-only http://127.0.0.1:PORT/path URL";
  local port_number="${value#http://127.0.0.1:}";
  port_number="${port_number%%/*}";
  (( port_number >= 1 && port_number <= 65535 )) || fail_usage "--health-url port must be between 1 and 65535";
}

validate_fixture() {
  case "${1}" in
    healthy|failed_config|permission_failure|dependency_failure|incomplete_bundle|ignored_directive) ;;
    *) fail_usage "--fixture must be healthy, failed_config, permission_failure, dependency_failure, incomplete_bundle, or ignored_directive" ;;
  esac
}

json_escape() {
  local value="${1}";
  value="${value//\\/\\\\}";
  value="${value//\"/\\\"}";
  value="${value//$'\n'/\\n}";
  value="${value//$'\r'/\\r}";
  value="${value//$'\t'/\\t}";
  printf '%s' "${value}";
}

safe_stem() {
  local value="${1}";
  value="${value%.service}";
  value="${value//[^A-Za-z0-9_.-]/_}";
  printf '%s' "${value}";
}

while [[ $# -gt 0 ]]; do
  case "${1}" in
    --unit)
      require_value "${1}" "${2:-}";
      UNIT_NAME="${2}";
      shift 2;
      ;;
    --output-dir)
      require_value "${1}" "${2:-}";
      OUTPUT_PARENT_DIRECTORY="${2}";
      shift 2;
      ;;
    --since)
      require_value "${1}" "${2:-}";
      JOURNAL_SINCE="${2}";
      shift 2;
      ;;
    --health-url)
      require_value "${1}" "${2:-}";
      HEALTH_URL="${2}";
      shift 2;
      ;;
    --path)
      require_value "${1}" "${2:-}";
      TARGET_PATH="${2}";
      shift 2;
      ;;
    --include-unit-verify)
      INCLUDE_UNIT_VERIFY=1;
      shift;
      ;;
    --fixture)
      require_value "${1}" "${2:-}";
      FIXTURE_SCENARIO="${2}";
      shift 2;
      ;;
    --dry-run)
      DRY_RUN=1;
      shift;
      ;;
    --help|-h)
      usage;
      exit 0;
      ;;
    *)
      fail_usage "unrecognized option: ${1}";
      ;;
  esac
done

[[ -n "${UNIT_NAME}" ]] || fail_usage "--unit is required";
[[ -n "${OUTPUT_PARENT_DIRECTORY}" ]] || fail_usage "--output-dir is required";
validate_unit "${UNIT_NAME}";
validate_since "${JOURNAL_SINCE}";
[[ -z "${HEALTH_URL}" ]] || validate_health_url "${HEALTH_URL}";
[[ -z "${FIXTURE_SCENARIO}" ]] || validate_fixture "${FIXTURE_SCENARIO}";

if [[ "${DRY_RUN}" -eq 1 ]]; then
  printf 'Collector plan (no commands will run):\n';
  printf '  unit: %s\n' "${UNIT_NAME}";
  printf '  output parent: %s\n' "${OUTPUT_PARENT_DIRECTORY}";
  printf '  journal since: %s\n' "${JOURNAL_SINCE}";
  printf '  health URL supplied: %s\n' "$([[ -n "${HEALTH_URL}" ]] && printf yes || printf no)";
  printf '  path metadata supplied: %s\n' "$([[ -n "${TARGET_PATH}" ]] && printf yes || printf no)";
  printf '  unit verification: %s\n' "$([[ "${INCLUDE_UNIT_VERIFY}" -eq 1 ]] && printf yes || printf no)";
  printf '  fixture scenario: %s\n' "${FIXTURE_SCENARIO:-none}";
  printf '  planned classes: metadata, systemctl_status, systemctl_show, systemctl_cat, journalctl_unit, process_metadata';
  [[ -n "${HEALTH_URL}" ]] && printf ', health_check';
  [[ -n "${TARGET_PATH}" ]] && printf ', path_metadata';
  [[ "${INCLUDE_UNIT_VERIFY}" -eq 1 ]] && printf ', unit_verify';
  printf '\n';
  exit 0;
fi

# A missing command should be an environment error, not an artifact that reads like
# evidence about the unit.
require_commands() {
  local missing_commands="";
  local command_name;
  for command_name in "$@"; do
    command -v "${command_name}" > /dev/null 2>&1 || missing_commands="${missing_commands} ${command_name}";
  done;
  [[ -z "${missing_commands}" ]] || fail_environment "required command(s) not found on PATH:${missing_commands}";
}

require_commands awk sed grep tr find date mkdir mktemp mv rm chmod cat head tail sha256sum wc id;
if [[ -z "${FIXTURE_SCENARIO}" ]]; then
  require_commands systemctl journalctl ps;
  [[ -z "${HEALTH_URL}" ]] || require_commands curl;
  [[ -z "${TARGET_PATH}" ]] || require_commands bash namei stat;
  [[ "${INCLUDE_UNIT_VERIFY}" -eq 0 ]] || require_commands systemd-analyze;
fi;

# Create the parent privately when it is absent, and leave an existing
# directory's mode alone. The operator's directory belongs to the operator; the
# bundle below it is the private object this tool owns.
if [[ -d "${OUTPUT_PARENT_DIRECTORY}" ]]; then
  :;
elif mkdir -p "${OUTPUT_PARENT_DIRECTORY}"; then
  chmod 700 "${OUTPUT_PARENT_DIRECTORY}" || fail_environment "cannot secure the output parent directory";
else
  fail_environment "cannot create the output parent directory";
fi;

COLLECTION_TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)";
# The suffix keeps two runs in the same second from colliding.
EVIDENCE_BUNDLE_DIRECTORY="${OUTPUT_PARENT_DIRECTORY}/evidence-${COLLECTION_TIMESTAMP}-$(safe_stem "${UNIT_NAME}")-$$";
mkdir "${EVIDENCE_BUNDLE_DIRECTORY}" || { printf 'Cannot create unique evidence bundle directory.\n' >&2; exit 3; }
chmod 700 "${EVIDENCE_BUNDLE_DIRECTORY}" \
  || { printf 'Environment error: cannot secure the evidence bundle directory.\n' >&2; exit 3; }
# The zone suffix is required: systemd reads a bare timestamp in the host's
# local zone, so "13:00:00" without it would silently mean local time.
JOURNAL_UNTIL_UTC="$(date -u +'%Y-%m-%d %H:%M:%S UTC')";
COMMAND_OUTCOMES_FILE="${EVIDENCE_BUNDLE_DIRECTORY}/command-outcomes.tsv";
printf 'artifact\tcommand_class\tstatus\texit_code\tnote\n' > "${COMMAND_OUTCOMES_FILE}" \
  || { printf 'Environment error: cannot create the command outcome record.\n' >&2; exit 3; }
chmod 600 "${COMMAND_OUTCOMES_FILE}" \
  || { printf 'Environment error: cannot secure the command outcome record.\n' >&2; exit 3; }

record_outcome() {
  local artifact="${1}";
  local command_class="${2}";
  local status="${3}";
  local exit_code="${4}";
  local note="${5}";
  note="${note//$'\t'/ }";
  note="${note//$'\n'/ }";
  note="${note//$'\r'/ }";
  # command-outcomes.tsv is the record that says how every other file should be
  # read. A silent failure to append to it would leave a bundle whose evidence
  # cannot be interpreted, so it ends the collection.
  printf '%s\t%s\t%s\t%s\t%s\n' "${artifact}" "${command_class}" "${status}" "${exit_code}" "${note}" >> "${COMMAND_OUTCOMES_FILE}" \
    || fail_environment "cannot append to the command outcome record";
}

# Publish a record on success only, the same contract redaction uses.
write_private_text_file() {
  local destination="${1}";
  local content="${2}";
  local temporary_file;
  temporary_file="$(mktemp "${EVIDENCE_BUNDLE_DIRECTORY}/.record.XXXXXX")" || return 1;
  if printf '%s' "${content}" > "${temporary_file}" \
    && chmod 600 "${temporary_file}" \
    && mv -- "${temporary_file}" "${destination}"; then
    return 0;
  fi;
  rm -f -- "${temporary_file}";
  return 1;
}

redact_to_file() {
  local source="${1}";
  local destination="${2}";
  # Narrow, best-effort redaction of selected assignment names. This is not a
  # secrets scanner. It deliberately over-redacts: once a name matches, the rest
  # of the line goes, because a systemd Environment= value may be quoted and
  # contain spaces, and a partly redacted secret is worse than a blanked line.
  local redacted_temporary_file;
  redacted_temporary_file="$(mktemp "${EVIDENCE_BUNDLE_DIRECTORY}/.redacted.XXXXXX")" || return 1;
  # Publish on success only. A truncated destination left behind by a failed
  # transformation would be an artifact that nothing wrote on purpose.
  if sed -E \
      -e 's/([A-Za-z0-9_]*(SECRET|TOKEN|PASSWORD|PASSWD|PASSPHRASE|API_KEY|APIKEY|PRIVATE_KEY|CLIENT_SECRET|BEARER|JWT|CREDENTIAL)[A-Za-z0-9_]*[[:space:]]*[=:]).*$/\1[REDACTED]/I' \
      -e 's|://[^/@[:space:]]+:[^/@[:space:]]+@|://[REDACTED]@|g' \
      "${source}" > "${redacted_temporary_file}" \
    && chmod 600 "${redacted_temporary_file}" \
    && mv -- "${redacted_temporary_file}" "${destination}"; then
    return 0;
  fi;
  rm -f -- "${redacted_temporary_file}";
  return 1;
}

# The note is passed in so that a live run never labels its own output as a
# fixture. An evidence file that misreports where it came from is worse than
# no evidence file.
capture_static_text() {
  local artifact="${1}";
  local command_class="${2}";
  local text="${3}";
  local note="${4:-synthetic fixture artifact}";
  local exit_code="${5:-0}";
  if write_private_text_file "${EVIDENCE_BUNDLE_DIRECTORY}/${artifact}" "${text}
"; then
    record_outcome "${artifact}" "${command_class}" "captured" "${exit_code}" "${note}";
  else
    record_outcome "${artifact}" "${command_class}" "integrity_error" "" "artifact could not be written or secured";
  fi;
}

capture_command() {
  local artifact="${1}";
  local command_class="${2}";
  shift 2;
  local temporary_file;
  local exit_code;
  temporary_file="$(mktemp "${EVIDENCE_BUNDLE_DIRECTORY}/.tmp.XXXXXX")" || { record_outcome "${artifact}" "${command_class}" "integrity_error" "" "cannot create temporary file"; return; }
  "$@" > "${temporary_file}" 2>&1;
  exit_code=$?;
  if redact_to_file "${temporary_file}" "${EVIDENCE_BUNDLE_DIRECTORY}/${artifact}"; then
    rm -f "${temporary_file}";
    record_outcome "${artifact}" "${command_class}" "captured" "${exit_code}" "command exit preserved as evidence";
  else
    rm -f "${temporary_file}";
    record_outcome "${artifact}" "${command_class}" "integrity_error" "${exit_code}" "redaction failed; artifact not retained";
  fi
}

# systemd-analyze verify reports ignored directives on stderr and still exits 0,
# so exit status alone cannot mark a unit as clean. Record the diagnostic line
# count beside the exit code so a reader of command-outcomes.tsv sees both.
record_unit_verify_note() {
  local exit_code="${1}";
  local diagnostic_line_count=0;
  if [[ -f "${EVIDENCE_BUNDLE_DIRECTORY}/unit-verify.txt" ]]; then
    diagnostic_line_count="$(grep -c '[^[:space:]]' "${EVIDENCE_BUNDLE_DIRECTORY}/unit-verify.txt" || true)";
  fi
  record_outcome "unit-verify.txt" "unit_verify" "captured" "${exit_code}" "exit ${exit_code}; ${diagnostic_line_count} diagnostic line(s); exit 0 with diagnostics is not a clean result";
}

# Same capture contract as capture_command, but the outcome note carries the
# diagnostic line count, and exactly one outcome row is written.
capture_unit_verify() {
  local fragment_path="${1}";
  local temporary_file;
  local exit_code;
  temporary_file="$(mktemp "${EVIDENCE_BUNDLE_DIRECTORY}/.tmp.XXXXXX")" || { record_outcome "unit-verify.txt" "unit_verify" "integrity_error" "" "cannot create temporary file"; return; }
  systemd-analyze verify "${fragment_path}" > "${temporary_file}" 2>&1;
  exit_code=$?;
  if redact_to_file "${temporary_file}" "${EVIDENCE_BUNDLE_DIRECTORY}/unit-verify.txt"; then
    rm -f "${temporary_file}";
    record_unit_verify_note "${exit_code}";
  else
    rm -f "${temporary_file}";
    record_outcome "unit-verify.txt" "unit_verify" "integrity_error" "${exit_code}" "redaction failed; artifact not retained";
  fi
}

write_fixture_bundle() {
  local scenario_name="${1}";
  local status_text="";
  local show_text="";
  local journal_text="";
  local process_text="";
  local health_text="";
  local health_stderr="";
  local path_text="";
  local unit_text="";
  local verify_text="";

  unit_text="[Unit]
Description=Example API fixture service

[Service]
Type=simple
User=example-api
Group=example-api
WorkingDirectory=/opt/example-api
EnvironmentFile=/etc/example-api/example-api.env
ExecStart=/usr/bin/python3 /opt/example-api/bin/example-api
Restart=on-failure"

  case "${scenario_name}" in
    healthy)
      status_text="● ${UNIT_NAME} - Example API fixture service
     Loaded: loaded (/etc/systemd/system/${UNIT_NAME}; enabled)
     Active: active (running) since 2026-08-18 20:00:00 UTC
   Main PID: 4242 (python3)"
      show_text="Id=${UNIT_NAME}
LoadState=loaded
ActiveState=active
SubState=running
Result=success
MainPID=4242
ExecMainCode=1
ExecMainStatus=0
NRestarts=0
Restart=on-failure
RestartUSec=100ms
TimeoutStartUSec=1min 30s
TimeoutStopUSec=1min 30s
User=example-api
Group=example-api
WorkingDirectory=/opt/example-api
FragmentPath=/etc/systemd/system/${UNIT_NAME}
DropInPaths="
      journal_text="2026-08-18T20:00:00+0000 labhost example-api[4242]: example-api started
2026-08-18T20:00:01+0000 labhost example-api[4242]: health endpoint ready"
      process_text="PID UID USER STAT ELAPSED %CPU %MEM
4242 1001 example-api Ssl 00:10 0.1 0.2"
      health_text="example-api-ok";
      ;;
    failed_config)
      status_text="● ${UNIT_NAME} - Example API fixture service
     Loaded: loaded (/etc/systemd/system/${UNIT_NAME}; enabled)
     Active: failed (Result: exit-code) since 2026-08-18 20:00:00 UTC
    Process: 4242 ExecStart=/usr/bin/python3 /opt/example-api/bin/example-api (code=exited, status=1/FAILURE)"
      show_text="Id=${UNIT_NAME}
LoadState=loaded
ActiveState=failed
SubState=failed
Result=exit-code
MainPID=0
ExecMainCode=1
ExecMainStatus=1
NRestarts=0
Restart=on-failure
RestartUSec=100ms
TimeoutStartUSec=1min 30s
TimeoutStopUSec=1min 30s
User=example-api
Group=example-api
WorkingDirectory=/opt/example-api
FragmentPath=/etc/systemd/system/${UNIT_NAME}
DropInPaths="
      journal_text="2026-08-18T20:00:00+0000 labhost example-api[4242]: config validation failed: invalid configuration EXAMPLE_API_MODE=invalid
2026-08-18T20:00:00+0000 labhost systemd[1]: ${UNIT_NAME}: Main process exited, code=exited, status=1/FAILURE"
      process_text="No active MainPID captured because the fixture service exited.";
      health_stderr="curl: (7) Failed to connect to 127.0.0.1 port 8088";
      ;;
    permission_failure)
      status_text="● ${UNIT_NAME} - Example API fixture service
     Loaded: loaded (/etc/systemd/system/${UNIT_NAME}; enabled)
     Active: failed (Result: exit-code) since 2026-08-18 20:00:00 UTC"
      show_text="Id=${UNIT_NAME}
LoadState=loaded
ActiveState=failed
SubState=failed
Result=exit-code
MainPID=0
ExecMainCode=1
ExecMainStatus=13
NRestarts=0
Restart=on-failure
RestartUSec=100ms
TimeoutStartUSec=1min 30s
TimeoutStopUSec=1min 30s
User=example-api
Group=example-api
WorkingDirectory=/opt/example-api
FragmentPath=/etc/systemd/system/${UNIT_NAME}
DropInPaths="
      journal_text="2026-08-18T20:00:00+0000 labhost example-api[4242]: permission denied reading /etc/example-api/example-api.env
2026-08-18T20:00:00+0000 labhost systemd[1]: ${UNIT_NAME}: Main process exited, code=exited, status=13/FAILURE"
      process_text="No active MainPID captured because the fixture service exited.";
      health_stderr="curl: (7) Failed to connect to 127.0.0.1 port 8088";
      path_text="f: /etc/example-api/example-api.env
 drwxr-xr-x root root /
 drwxr-xr-x root root etc
 drwxr-x--- root root example-api
 -rw------- root root example-api.env
root:root 600 /etc/example-api/example-api.env"
      ;;
    dependency_failure)
      status_text="● ${UNIT_NAME} - Example API fixture service
     Loaded: loaded (/etc/systemd/system/${UNIT_NAME}; enabled)
     Active: active (running) since 2026-08-18 20:00:00 UTC
   Main PID: 4242 (python3)"
      show_text="Id=${UNIT_NAME}
LoadState=loaded
ActiveState=active
SubState=running
Result=success
MainPID=4242
ExecMainCode=1
ExecMainStatus=0
NRestarts=0
Restart=on-failure
RestartUSec=100ms
TimeoutStartUSec=1min 30s
TimeoutStopUSec=1min 30s
User=example-api
Group=example-api
WorkingDirectory=/opt/example-api
FragmentPath=/etc/systemd/system/${UNIT_NAME}
DropInPaths="
      journal_text="2026-08-18T20:00:00+0000 labhost example-api[4242]: dependency connection refused at 127.0.0.1:9099
2026-08-18T20:00:02+0000 labhost example-api[4242]: health status degraded because dependency is unavailable"
      process_text="PID UID USER STAT ELAPSED %CPU %MEM
4242 1001 example-api Ssl 00:10 0.1 0.2"
      health_stderr="curl: (22) The requested URL returned error: 503";
      ;;
    incomplete_bundle)
      status_text="● ${UNIT_NAME} - Example API fixture service
     Loaded: loaded (/etc/systemd/system/${UNIT_NAME}; enabled)
     Active: failed (Result: exit-code) since 2026-08-18 20:00:00 UTC"
      show_text="Id=${UNIT_NAME}
LoadState=loaded
ActiveState=failed
SubState=failed
Result=exit-code
MainPID=0
ExecMainCode=1
ExecMainStatus=1
NRestarts=1
Restart=on-failure
RestartUSec=100ms
TimeoutStartUSec=1min 30s
TimeoutStopUSec=1min 30s
User=example-api
Group=example-api
WorkingDirectory=/opt/example-api
FragmentPath=/etc/systemd/system/${UNIT_NAME}
DropInPaths="
      journal_text="";
      process_text="No active MainPID captured because the fixture service exited.";
      health_stderr="curl: (7) Failed to connect to 127.0.0.1 port 8088";
      ;;
    ignored_directive)
      unit_text="${unit_text%Restart=on-failure}Restart=on-failuer";
      verify_text="/etc/systemd/system/${UNIT_NAME}:11: Failed to parse service restart specifier, ignoring: on-failuer";
      status_text="● ${UNIT_NAME} - Example API fixture service
     Loaded: loaded (/etc/systemd/system/${UNIT_NAME}; enabled)
     Active: failed (Result: signal) since 2026-08-18 20:00:00 UTC
    Process: 4242 ExecStart=/usr/bin/python3 /opt/example-api/bin/example-api (code=killed, signal=KILL)"
      show_text="Id=${UNIT_NAME}
LoadState=loaded
ActiveState=failed
SubState=failed
Result=signal
MainPID=0
ExecMainCode=2
ExecMainStatus=9
NRestarts=0
Restart=no
RestartUSec=100ms
TimeoutStartUSec=1min 30s
TimeoutStopUSec=1min 30s
User=example-api
Group=example-api
WorkingDirectory=/opt/example-api
FragmentPath=/etc/systemd/system/${UNIT_NAME}
DropInPaths="
      journal_text="2026-08-18T20:00:00+0000 labhost systemd[1]: ${UNIT_NAME}: Main process exited, code=killed, status=9/KILL
2026-08-18T20:00:00+0000 labhost systemd[1]: ${UNIT_NAME}: Failed with result 'signal'."
      process_text="No active MainPID captured because the fixture service exited.";
      health_stderr="curl: (7) Failed to connect to 127.0.0.1 port 8088";
      ;;
  esac

  capture_static_text "systemctl-status.txt" "systemctl_status" "${status_text}";
  capture_static_text "systemctl-show.txt" "systemctl_show" "${show_text}";
  capture_static_text "unit-effective.txt" "systemctl_cat" "${unit_text}";
  if [[ "${scenario_name}" == "incomplete_bundle" ]]; then
    record_outcome "journal.txt" "journalctl_unit" "unavailable" "" "synthetic incomplete bundle omits journal artifact";
  else
    capture_static_text "journal.txt" "journalctl_unit" "${journal_text}";
  fi
  capture_static_text "process.txt" "process_metadata" "${process_text}";
  if [[ -n "${health_text}" ]]; then
    capture_static_text "health.txt" "health_check" "${health_text}";
    capture_static_text "health.stderr" "health_check" "" "synthetic fixture stderr";
  else
    capture_static_text "health.txt" "health_check" "" "synthetic non-success health evidence" "22";
    capture_static_text "health.stderr" "health_check" "${health_stderr}" "synthetic health stderr" "22";
  fi
  if [[ -n "${path_text}" ]]; then
    capture_static_text "path-metadata.txt" "path_metadata" "${path_text}";
  else
    record_outcome "path-metadata.txt" "path_metadata" "skipped" "" "fixture scenario has no path metadata request";
  fi
  if [[ "${INCLUDE_UNIT_VERIFY}" -eq 1 ]]; then
    if write_private_text_file "${EVIDENCE_BUNDLE_DIRECTORY}/unit-verify.txt" "${verify_text:+${verify_text}
}"; then
      record_unit_verify_note "0";
    else
      record_outcome "unit-verify.txt" "unit_verify" "integrity_error" "" "artifact could not be written or secured";
    fi;
  else
    record_outcome "unit-verify.txt" "unit_verify" "skipped" "" "--include-unit-verify not supplied";
  fi
}

collect_live_bundle() {
  capture_command "systemctl-status.txt" "systemctl_status" systemctl status "${UNIT_NAME}" --no-pager;
  capture_command "systemctl-show.txt" "systemctl_show" systemctl show "${UNIT_NAME}" \
    --property=Id,LoadState,ActiveState,SubState,Result,MainPID,ExecMainCode,ExecMainStatus,NRestarts,Restart,RestartUSec,TimeoutStartUSec,TimeoutStopUSec,User,Group,WorkingDirectory,FragmentPath,DropInPaths;
  capture_command "unit-effective.txt" "systemctl_cat" systemctl cat "${UNIT_NAME}";
  # --until fixes the far edge of the window, so the slice cannot quietly grow
  # to include entries written after collection started.
  capture_command "journal.txt" "journalctl_unit" journalctl --unit="${UNIT_NAME}" --since="${JOURNAL_SINCE}" --until="${JOURNAL_UNTIL_UTC}" --no-pager --output=short-iso;

  local main_process_id;
  main_process_id="$(awk -F= '$1=="MainPID" {print $2}' "${EVIDENCE_BUNDLE_DIRECTORY}/systemctl-show.txt" | tr -d '[:space:]' | head -n 1)";
  if [[ "${main_process_id}" =~ ^[1-9][0-9]*$ ]]; then
    capture_command "process.txt" "process_metadata" ps -p "${main_process_id}" -o pid=,uid=,user=,stat=,etime=,%cpu=,%mem=;
  else
    capture_static_text "process.txt" "process_metadata" "No positive MainPID was captured from selected unit properties." "written by the collector; no process to query";
  fi

  if [[ -n "${HEALTH_URL}" ]]; then
    local health_temporary_file;
    if ! health_temporary_file="$(mktemp "${EVIDENCE_BUNDLE_DIRECTORY}/.health.XXXXXX")"; then
      # Recorded and skipped, never returned from: a health failure must not
      # stop the collector from attempting the other evidence the operator asked
      # for, or command-outcomes.tsv stops being a record of what was attempted.
      record_outcome "health.txt" "health_check" "integrity_error" "" "cannot create temporary health file";
      record_outcome "health.stderr" "health_check" "integrity_error" "" "cannot create temporary health file";
      health_temporary_file="";
    fi;
    if [[ -n "${health_temporary_file}" ]]; then
    # -q must come first: without it curl reads ~/.curlrc and treats its lines as
    # command-line options, so a file on the host could turn this into a POST, a
    # second URL, or a write outside the bundle. --noproxy keeps a proxy variable
    # in the environment from sending a loopback URL off the host.
    curl -q --noproxy '*' --fail --silent --show-error --max-time 5 --max-redirs 0 "${HEALTH_URL}" > "${health_temporary_file}" 2> "${EVIDENCE_BUNDLE_DIRECTORY}/.health.stderr";
    local health_exit_code=$?;
    if redact_to_file "${health_temporary_file}" "${EVIDENCE_BUNDLE_DIRECTORY}/health.txt" \
      && redact_to_file "${EVIDENCE_BUNDLE_DIRECTORY}/.health.stderr" "${EVIDENCE_BUNDLE_DIRECTORY}/health.stderr"; then
      record_outcome "health.txt" "health_check" "captured" "${health_exit_code}" "loopback health request exit preserved as evidence";
      record_outcome "health.stderr" "health_check" "captured" "${health_exit_code}" "loopback health stderr preserved as evidence";
    else
      rm -f "${EVIDENCE_BUNDLE_DIRECTORY}/health.txt" "${EVIDENCE_BUNDLE_DIRECTORY}/health.stderr";
      record_outcome "health.txt" "health_check" "integrity_error" "${health_exit_code}" "redaction failed; artifact not retained";
      record_outcome "health.stderr" "health_check" "integrity_error" "${health_exit_code}" "redaction failed; artifact not retained";
    fi;
    rm -f "${health_temporary_file}" "${EVIDENCE_BUNDLE_DIRECTORY}/.health.stderr";
    fi;
  else
    record_outcome "health.txt" "health_check" "skipped" "" "--health-url not supplied";
  fi

  if [[ -n "${TARGET_PATH}" ]]; then
    # $1 expands in the child shell on purpose: the path travels as an
    # argument and is never spliced into the command string.
    # shellcheck disable=SC2016
    capture_command "path-metadata.txt" "path_metadata" bash -c 'namei -l -- "${1}"; stat --format="%U:%G %a %n" -- "${1}"' _ "${TARGET_PATH}";
  else
    record_outcome "path-metadata.txt" "path_metadata" "skipped" "" "--path not supplied";
  fi

  if [[ "${INCLUDE_UNIT_VERIFY}" -eq 1 ]]; then
    local fragment_path;
    fragment_path="$(awk -F= '$1=="FragmentPath" {print $2}' "${EVIDENCE_BUNDLE_DIRECTORY}/systemctl-show.txt" | head -n 1)";
    if [[ -n "${fragment_path}" && -f "${fragment_path}" ]]; then
      capture_unit_verify "${fragment_path}";
    else
      record_outcome "unit-verify.txt" "unit_verify" "unavailable" "" "unit fragment path not available as a regular file";
    fi
  else
    record_outcome "unit-verify.txt" "unit_verify" "skipped" "" "--include-unit-verify not supplied";
  fi
}

write_collector_summary() {
  local active_state="unknown";
  local unit_result="unknown";
  local health_note="not requested";
  [[ -f "${EVIDENCE_BUNDLE_DIRECTORY}/systemctl-show.txt" ]] && active_state="$(awk -F= '$1=="ActiveState" {print $2}' "${EVIDENCE_BUNDLE_DIRECTORY}/systemctl-show.txt" | head -n 1)";
  [[ -f "${EVIDENCE_BUNDLE_DIRECTORY}/systemctl-show.txt" ]] && unit_result="$(awk -F= '$1=="Result" {print $2}' "${EVIDENCE_BUNDLE_DIRECTORY}/systemctl-show.txt" | head -n 1)";
  if [[ -n "${HEALTH_URL}" || -n "${FIXTURE_SCENARIO}" ]]; then
    local health_exit_code health_status;
    health_status="$(awk -F'\t' '$1=="health.txt" {print $3}' "${COMMAND_OUTCOMES_FILE}" | tail -n 1)";
    health_exit_code="$(awk -F'\t' '$1=="health.txt" {print $4}' "${COMMAND_OUTCOMES_FILE}" | tail -n 1)";
    case "${health_status}" in
      captured) health_note="captured with exit code ${health_exit_code:-unknown}; a nonzero exit can be target evidence";;
      integrity_error) health_note="integrity error; the artifact was not retained";;
      skipped) health_note="skipped";;
      unavailable) health_note="unavailable";;
      *) health_note="no outcome recorded";;
    esac;
  fi
  local summary_content;
  summary_content="$(cat <<EOF
# Evidence Collection Summary

- **Collector version:** ${COLLECTOR_VERSION}
- **Unit:** ${UNIT_NAME}
- **Collected at (UTC):** ${COLLECTION_TIMESTAMP}
- **Mode:** $([[ -n "${FIXTURE_SCENARIO}" ]] && printf 'synthetic fixture (%s)' "${FIXTURE_SCENARIO}" || printf 'live read-only collection')
- **Captured ActiveState:** ${active_state:-unknown}
- **Captured Result:** ${unit_result:-unknown}
- **Health evidence:** ${health_note}

This bundle is observational. It does not identify a root cause and does not authorize a restart,
reload, reset, or other state-changing action. Review it with docs/review_guide.md
in the repository that produced this bundle.
EOF
)";
  write_private_text_file "${EVIDENCE_BUNDLE_DIRECTORY}/collector-summary.md" "${summary_content}
" || return 1;
}

# A non-root caller outside systemd-journal sees a filtered journal, and the
# filtering is silent. Recording the privilege context lets a reader tell a
# quiet slice from an empty one.
journal_access_note() {
  if [[ -n "${FIXTURE_SCENARIO}" ]]; then
    printf 'not_applicable_fixture';
  elif [[ "$(id -u)" -eq 0 ]]; then
    printf 'full_as_root';
  elif id -nG 2>/dev/null | tr ' ' '\n' | grep -qx 'systemd-journal'; then
    printf 'full_via_systemd_journal_group';
  elif id -nG 2>/dev/null | tr ' ' '\n' | grep -qx 'adm'; then
    printf 'full_via_adm_group';
  else
    printf 'possibly_restricted';
  fi;
}

# The status is derived, never asserted. A bundle that lost artifacts says so.
# Three states, defined in README:
#   complete                       every attempted command produced its artifact at exit 0
#   complete_with_command_failures every artifact exists, but a command exited nonzero
#   partial                        an artifact the collector attempted is absent
collection_status_value() {
  local missing_artifact_count nonzero_exit_count;
  missing_artifact_count="$(awk -F'\t' 'NR>1 && ($3=="integrity_error" || $3=="unavailable") {n++} END {print n+0}' "${COMMAND_OUTCOMES_FILE}")";
  nonzero_exit_count="$(awk -F'\t' 'NR>1 && $3=="captured" && $4 != "" && $4 != "0" {n++} END {print n+0}' "${COMMAND_OUTCOMES_FILE}")";
  if [[ "${missing_artifact_count}" -gt 0 ]]; then
    printf 'partial';
  elif [[ "${nonzero_exit_count}" -gt 0 ]]; then
    printf 'complete_with_command_failures';
  else
    printf 'complete';
  fi;
}

# The host's systemd version decides how to read the property names in a bundle.
host_systemd_version() {
  local version_text="";
  if [[ -n "${FIXTURE_SCENARIO}" ]]; then
    printf 'not_applicable_fixture';
    return;
  fi;
  version_text="$(systemctl --version 2>/dev/null | head -n 1)";
  printf '%s' "${version_text:-unknown}";
}

write_collection_json() {
  local artifact_count_value="${1}";
  local collection_mode="live_read_only";
  [[ -n "${FIXTURE_SCENARIO}" ]] && collection_mode="synthetic_fixture";
  local collection_metadata;
  collection_metadata="$(cat <<EOF
{
  "schema_version": "1.2",
  "collector_version": "${COLLECTOR_VERSION}",
  "collected_at_utc": "${COLLECTION_TIMESTAMP}",
  "unit": "$(json_escape "${UNIT_NAME}")",
  "journal_since": "$(json_escape "${JOURNAL_SINCE}")",
  "journal_until_utc": "$(json_escape "${JOURNAL_UNTIL_UTC}")",
  "inputs": {
    "health_url_supplied": $([[ -n "${HEALTH_URL}" ]] && printf true || printf false),
    "health_target": "$(json_escape "${HEALTH_URL:-none}")",
    "path_supplied": $([[ -n "${TARGET_PATH}" ]] && printf true || printf false),
    "include_unit_verify": $([[ "${INCLUDE_UNIT_VERIFY}" -eq 1 ]] && printf true || printf false)
  },
  "dry_run": false,
  "mode": "${collection_mode}",
  "fixture_evidence": {
    "health_probe_simulated": $([[ -n "${FIXTURE_SCENARIO}" ]] && printf true || printf false),
    "path_metadata_simulated": $([[ "${FIXTURE_SCENARIO}" == "permission_failure" ]] && printf true || printf false)
  },
  "fixture_scenario": "$(json_escape "${FIXTURE_SCENARIO:-}")",
  "artifact_count": ${artifact_count_value},
  "collection_status": "$(collection_status_value)",
  "collection_context": {
    "euid": $(id -u),
    "journal_access": "$(journal_access_note)",
    "systemd_version": "$(json_escape "$(host_systemd_version)")"
  },
  "privacy": {
    "output_mode": "0700",
    "redaction_filter": "assignment_patterns_v2",
    "redaction_scope": "narrow best-effort blanking of assignments whose name contains SECRET, TOKEN, PASSWORD, PASSWD, PASSPHRASE, API_KEY, APIKEY, PRIVATE_KEY, CLIENT_SECRET, BEARER, JWT or CREDENTIAL, plus user:password pairs inside URLs; the rest of a matched assignment line is blanked; this is not a secrets scanner",
    "hostname_redaction_applied": false,
    "hostname_may_be_present": true
  }
}
EOF
)";
  write_private_text_file "${EVIDENCE_BUNDLE_DIRECTORY}/collection.json" "${collection_metadata}
" || return 1;
}

# The manifest covers the collection records as well as the evidence, because a
# reader who can change command-outcomes.tsv or collection.json can change what
# the evidence appears to mean.
# Every digest and byte count is checked before it reaches the manifest. A blank
# field here would be a manifest row that proves nothing while looking complete.
manifest_row() {
  local artifact="${1}";
  local command_class="${2}";
  local exit_code="${3}";
  local checksum_value byte_count;
  checksum_value="$(sha256sum "${EVIDENCE_BUNDLE_DIRECTORY}/${artifact}" | awk '{print $1}')" || return 1;
  [[ "${checksum_value}" =~ ^[0-9a-f]{64}$ ]] || return 1;
  byte_count="$(wc -c < "${EVIDENCE_BUNDLE_DIRECTORY}/${artifact}" | tr -d '[:space:]')" || return 1;
  [[ "${byte_count}" =~ ^[0-9]+$ ]] || return 1;
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "${artifact}" "${checksum_value}" "${byte_count}" "${COLLECTION_TIMESTAMP}" "${command_class}" "${exit_code}";
}

write_manifest() {
  local manifest_file="${EVIDENCE_BUNDLE_DIRECTORY}/manifest.tsv";
  local manifest_temporary_file record_file;
  manifest_temporary_file="$(mktemp "${EVIDENCE_BUNDLE_DIRECTORY}/.manifest.XXXXXX")" || return 1;
  printf 'artifact\tsha256\tbytes\tcollected_at_utc\tcommand_class\tcommand_exit\n' > "${manifest_temporary_file}" \
    || { rm -f -- "${manifest_temporary_file}"; return 1; };
  for record_file in command-outcomes.tsv collection.json collector-summary.md; do
    # These records are required by the bundle format. A missing one means the
    # collection failed, so finalization fails rather than publishing a bundle
    # the analyzer will reject.
    [[ -f "${EVIDENCE_BUNDLE_DIRECTORY}/${record_file}" ]] \
      || { rm -f -- "${manifest_temporary_file}"; return 1; };
    manifest_row "${record_file}" "collection_record" "" >> "${manifest_temporary_file}" \
      || { rm -f -- "${manifest_temporary_file}"; return 1; };
  done;
  while IFS=$'\t' read -r artifact command_class status exit_code note; do
    [[ "${artifact}" == "artifact" ]] && continue;
    [[ "${status}" == "captured" ]] || continue;
    # An artifact recorded as captured that is no longer on disk is a
    # contradiction the collector detected itself. Fail instead of skipping it.
    [[ -f "${EVIDENCE_BUNDLE_DIRECTORY}/${artifact}" ]] \
      || { rm -f -- "${manifest_temporary_file}"; return 1; };
    manifest_row "${artifact}" "${command_class}" "${exit_code}" >> "${manifest_temporary_file}" \
      || { rm -f -- "${manifest_temporary_file}"; return 1; };
  done < "${COMMAND_OUTCOMES_FILE}";
  chmod 600 "${manifest_temporary_file}" || { rm -f -- "${manifest_temporary_file}"; return 1; };
  mv -- "${manifest_temporary_file}" "${manifest_file}" || { rm -f -- "${manifest_temporary_file}"; return 1; };
}

if [[ -n "${FIXTURE_SCENARIO}" ]]; then
  write_fixture_bundle "${FIXTURE_SCENARIO}";
else
  collect_live_bundle;
fi

write_collector_summary || fail_environment "cannot write the collector summary";
# Evidence artifacts and collection records, excluding the manifest, its anchor,
# and collection.json itself. The exclusions are named rather than implied by
# write order, so the analyzer's identical definition cannot drift from this one.
ARTIFACT_COUNT_VALUE="$(find "${EVIDENCE_BUNDLE_DIRECTORY}" -maxdepth 1 -type f \
  ! -name 'manifest.tsv' ! -name 'manifest.sha256' ! -name 'collection.json' \
  | wc -l | tr -d '[:space:]')" \
  || fail_environment "cannot count the collected artifacts";
write_collection_json "${ARTIFACT_COUNT_VALUE}" || fail_environment "cannot write the collection metadata";
write_manifest || fail_environment "cannot write the manifest";

MANIFEST_DIGEST_VALUE="$(sha256sum "${EVIDENCE_BUNDLE_DIRECTORY}/manifest.tsv" | awk '{print $1}')" \
  || fail_environment "cannot calculate the manifest digest";
[[ "${MANIFEST_DIGEST_VALUE}" =~ ^[0-9a-f]{64}$ ]] || fail_environment "the manifest digest is malformed";
write_private_text_file "${EVIDENCE_BUNDLE_DIRECTORY}/manifest.sha256" "${MANIFEST_DIGEST_VALUE}  manifest.tsv
" || fail_environment "cannot write the manifest anchor";

printf 'Evidence bundle created: %s\n' "${EVIDENCE_BUNDLE_DIRECTORY}";
printf 'manifest.tsv sha256: %s\n' "${MANIFEST_DIGEST_VALUE}";
printf 'Record that digest outside this host if the bundle will travel.\n';
printf 'Review collector-summary.md and then use the diagnostic playbook; this tool does not perform recovery.\n';
exit 0;
