#!/usr/bin/env python3
"""Read a local evidence bundle and produce conservative review reports.

Safety boundary:
- Reads only the explicit bundle directory.
- Does not invoke subprocesses, network requests, or system-management APIs.
- Does not prescribe state-changing operations or assert a root cause.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

ANALYZER_VERSION = "0.11.0"
SCHEMA_VERSION = "1.2"
# Only the schema this validator actually implements. Earlier bundles kept the
# collection records outside the manifest, so the coverage rules below would
# reject them; claiming support for them would be a promise the code breaks.
SUPPORTED_SCHEMA_VERSIONS = ("1.2",)
OUTCOME_COLUMNS = ("artifact", "command_class", "status", "exit_code", "note")
MANIFEST_COLUMNS = ("artifact", "sha256", "bytes", "collected_at_utc", "command_class", "command_exit")
VALID_OUTCOME_STATUSES = ("captured", "skipped", "unavailable", "integrity_error")
# The vocabularies collection.json is allowed to use. They exist so that
# metadata_valid means the record says something this analyzer can act on,
# rather than only that it parsed as JSON.
VALID_COLLECTION_MODES = ("live_read_only", "synthetic_fixture")
VALID_COLLECTION_STATUSES = ("complete", "complete_with_command_failures", "partial")
VALID_JOURNAL_ACCESS_VALUES = (
    "full_as_root",
    "full_via_systemd_journal_group",
    "full_via_adm_group",
    "possibly_restricted",
    "not_applicable_fixture",
)


def valid_process_exit(value: str) -> bool:
    """A recorded exit status is a shell process status or nothing at all."""
    if value == "":
        return True
    if not value.isdigit():
        return False
    return 0 <= int(value) <= 255


def valid_outcome_exit(status: str, value: str) -> bool:
    """Whether an exit status is the one its outcome status is allowed to carry.

    A blank exit on a captured row is the hole this closes. Without the status
    in the rule, a row can claim a command ran and produced an artifact while
    recording nothing about how that command ended, and the artifact still
    becomes readable. A captured row must say how the command exited.

    Rows that record no command run carry no exit status. integrity_error is
    the one status that legitimately goes both ways: the failure can happen
    before the command runs, or after it has already exited.
    """
    if status == "captured":
        return value != "" and valid_process_exit(value)
    if status in ("skipped", "unavailable"):
        return value == ""
    return valid_process_exit(value)


SAFE_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*\.service$")


@dataclass(frozen=True)
class Observation:
    """A limited, evidence-linked fact that requires human review."""

    id: str
    severity: str
    statement: str
    evidence: list[str]
    playbook_reference: str


@dataclass
class DraftValidation:
    """Findings still being collected. Mutable on purpose, and never reported."""

    limitations: list[str]
    command_exit_notes: list[str]
    errors: list[str]
    manifest_valid: bool
    outcomes_valid: bool
    metadata_valid: bool
    verified_manifest_names: set[str]
    validated_outcomes: dict[str, dict[str, str]]


@dataclass(frozen=True)
class ValidationResult:
    """Bundle contract validation result."""

    status: str
    limitations: tuple[str, ...]
    errors: tuple[str, ...]
    manifest_valid: bool
    outcomes_valid: bool = True
    evidence_incomplete: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate a local evidence bundle and generate conservative human-review reports."
    )
    parser.add_argument("--bundle", required=True, help="Explicit evidence bundle directory.")
    parser.add_argument("--output-dir", required=True, help="Parent directory for generated report.")
    return parser.parse_args()


def utc_now_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Required JSON artifact is unavailable or unsafe: {path.name}")
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must contain an object: {path.name}")
    return value


def read_text(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Artifact is unavailable or unsafe: {path.name}")
    return path.read_text(encoding="utf-8", errors="replace")


def parse_tsv(path: Path, expected_columns: tuple[str, ...]) -> list[dict[str, str]]:
    """Parse a TSV artifact, rejecting anything that is not exactly the contract.

    A malformed row must produce an invalid bundle, never a traceback, so the
    header is matched exactly and every cell is required to be a string.
    """
    text = read_text(path)
    lines = text.splitlines()
    if not lines:
        raise ValueError(f"TSV artifact is empty: {path.name}")
    header = tuple(lines[0].split("\t"))
    if header != expected_columns:
        raise ValueError(
            f"{path.name} header does not match the expected columns "
            f"{list(expected_columns)}; found {list(header)}."
        )
    rows: list[dict[str, str]] = []
    for number, line in enumerate(lines[1:], start=2):
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) != len(expected_columns):
            raise ValueError(
                f"{path.name} line {number} has {len(cells)} fields; expected {len(expected_columns)}."
            )
        rows.append(dict(zip(expected_columns, cells)))
    return rows


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 128), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_collection_metadata(metadata: dict[str, Any], limitations: list[str], errors: list[str]) -> bool:
    """Return whether the metadata conforms to the contract.

    Digest verification proves the bytes are the ones collected. It proves
    nothing about whether those bytes say something sensible, so semantic
    validity is tracked on its own.
    """
    error_count_before = len(errors)
    required_string_fields = [
        "schema_version",
        "collector_version",
        "collected_at_utc",
        "unit",
        "journal_since",
        "journal_until_utc",
        "mode",
    ]
    for field in required_string_fields:
        if not isinstance(metadata.get(field), str) or not metadata[field].strip():
            errors.append(f"collection.json is missing required non-empty string field: {field}")
    schema = metadata.get("schema_version")
    if schema not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append(
            f"Unsupported collection schema version: {schema!r}; supported: {', '.join(SUPPORTED_SCHEMA_VERSIONS)}."
        )

    # A malformed nested object must become a validation error, never an
    # AttributeError halfway through rendering the report.
    for field in ("collection_context", "inputs", "privacy", "fixture_evidence"):
        value = metadata.get(field)
        if value is not None and not isinstance(value, dict):
            errors.append(f"collection.json field {field} must be an object.")
            metadata[field] = {}

    # Every field below is one the analyzer reads, displays, or reasons from.
    # Verifying the digest proves the bytes are the collected ones; only this
    # checks that those bytes carry values the rest of the tool can act on. A
    # field the schema does not name is left alone, so a newer collector can add
    # one without this validator calling the bundle invalid.
    mode = metadata.get("mode")
    if isinstance(mode, str) and mode not in VALID_COLLECTION_MODES:
        errors.append(
            f"collection.json mode is not a recognized collection mode: {mode!r}; "
            f"expected one of {', '.join(VALID_COLLECTION_MODES)}."
        )
    status_field = metadata.get("collection_status")
    if not isinstance(status_field, str) or status_field not in VALID_COLLECTION_STATUSES:
        errors.append(
            f"collection.json collection_status is not a recognized status: {status_field!r}; "
            f"expected one of {', '.join(VALID_COLLECTION_STATUSES)}."
        )
    count_field = metadata.get("artifact_count")
    # isinstance(True, int) is true in Python, and a boolean artifact count is
    # not a count. The bool check has to come first.
    if isinstance(count_field, bool) or not isinstance(count_field, int) or count_field < 0:
        errors.append("collection.json artifact_count must be a nonnegative integer.")
    if "dry_run" in metadata and not isinstance(metadata["dry_run"], bool):
        errors.append("collection.json dry_run must be a boolean.")
    if "fixture_scenario" in metadata and not isinstance(metadata["fixture_scenario"], str):
        errors.append("collection.json fixture_scenario must be a string.")

    inputs = metadata.get("inputs")
    if isinstance(inputs, dict):
        for field in ("health_url_supplied", "path_supplied", "include_unit_verify"):
            if field in inputs and not isinstance(inputs[field], bool):
                errors.append(f"collection.json inputs.{field} must be a boolean.")
        if "health_target" in inputs and not isinstance(inputs["health_target"], str):
            errors.append("collection.json inputs.health_target must be a string.")

    fixture_evidence = metadata.get("fixture_evidence")
    if isinstance(fixture_evidence, dict):
        for field in ("health_probe_simulated", "path_metadata_simulated"):
            if field in fixture_evidence and not isinstance(fixture_evidence[field], bool):
                errors.append(f"collection.json fixture_evidence.{field} must be a boolean.")

    context_field = metadata.get("collection_context")
    if isinstance(context_field, dict):
        euid = context_field.get("euid")
        if euid is not None and (isinstance(euid, bool) or not isinstance(euid, int) or euid < 0):
            errors.append("collection.json collection_context.euid must be a nonnegative integer.")
        access = context_field.get("journal_access")
        if access is not None and access not in VALID_JOURNAL_ACCESS_VALUES:
            errors.append(
                f"collection.json collection_context.journal_access is not a recognized value: {access!r}."
            )
        systemd_version = context_field.get("systemd_version")
        if systemd_version is not None and not isinstance(systemd_version, str):
            errors.append("collection.json collection_context.systemd_version must be a string.")

    context = metadata.get("collection_context")
    if isinstance(context, dict) and context.get("journal_access") == "possibly_restricted":
        limitations.append(
            "Collected by a non-root caller outside the systemd-journal and adm groups. "
            "The journal slice may omit entries that exist on the host."
        )
    status_value = metadata.get("collection_status", "")
    if isinstance(status_value, str) and status_value.startswith("partial"):
        limitations.append(f"The collector recorded its own run as {status_value}.")
    unit = metadata.get("unit", "")
    if isinstance(unit, str) and not SAFE_UNIT_RE.fullmatch(unit):
        errors.append("collection.json unit does not meet the expected literal .service naming rule.")
    privacy = metadata.get("privacy")
    if not isinstance(privacy, dict):
        limitations.append("Privacy metadata is absent or malformed; handle bundle contents as potentially sensitive.")
    else:
        if not (privacy.get("redaction_applied") is True or isinstance(privacy.get("redaction_filter"), str)):
            limitations.append("Bundle names no redaction filter; handle its contents as unredacted.")
        if privacy.get("output_mode") != "0700":
            limitations.append("Bundle does not confirm the expected private output directory mode (0700).")
    return len(errors) == error_count_before


def validate_bundle(bundle: Path, metadata: dict[str, Any]) -> tuple["DraftValidation", list[dict[str, str]], list[dict[str, str]]]:
    limitations: list[str] = []
    command_exit_notes: list[str] = []
    errors: list[str] = []
    metadata_valid = validate_collection_metadata(metadata, limitations, errors)

    required_names = {"command-outcomes.tsv", "manifest.tsv", "systemctl-show.txt", "systemctl-status.txt"}
    for name in required_names:
        artifact = bundle / name
        if artifact.is_symlink() or not artifact.is_file():
            errors.append(f"Required artifact missing or unsafe: {name}")

    outcomes: list[dict[str, str]] = []
    manifest_rows: list[dict[str, str]] = []
    try:
        outcomes = parse_tsv(bundle / "command-outcomes.tsv", OUTCOME_COLUMNS)
    except ValueError as exc:
        errors.append(str(exc))
    manifest_valid = True
    try:
        manifest_rows = parse_tsv(bundle / "manifest.tsv", MANIFEST_COLUMNS)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        manifest_valid = False

    # The outcome record decides what every artifact means, so its own validity
    # is part of the trust decision. A malformed outcome table cannot be allowed
    # to vouch for content just because the manifest happens to be intact.
    outcomes_valid = True
    outcome_names = [row["artifact"] for row in outcomes]
    for duplicate in sorted({name for name in outcome_names if outcome_names.count(name) > 1}):
        errors.append(f"command-outcomes.tsv records {duplicate} more than once.")
        outcomes_valid = False
    for row in outcomes:
        if not row["artifact"] or Path(row["artifact"]).name != row["artifact"]:
            errors.append(f"command-outcomes.tsv contains an unsafe artifact name: {row['artifact']!r}")
            outcomes_valid = False
        if row["status"] not in VALID_OUTCOME_STATUSES:
            errors.append(f"command-outcomes.tsv contains an unrecognized status: {row['status']!r}")
            outcomes_valid = False
        if not valid_outcome_exit(row["status"], row["exit_code"]):
            errors.append(
                f"command-outcomes.tsv records {row['artifact']!r} as {row['status']!r} "
                f"with exit status {row['exit_code']!r}, which that status does not permit."
            )
            outcomes_valid = False
    if not outcomes:
        errors.append("command-outcomes.tsv contains no outcome records.")
        outcomes_valid = False
    if not manifest_rows and (bundle / "manifest.tsv").is_file():
        errors.append("manifest.tsv contains no artifact records.")
        manifest_valid = False

    outcome_by_artifact = {row.get("artifact", ""): row for row in outcomes}
    for optional in ("journal.txt", "health.txt", "path-metadata.txt", "unit-verify.txt"):
        if optional not in outcome_by_artifact:
            limitations.append(f"No collection outcome is recorded for optional artifact: {optional}")
        elif outcome_by_artifact[optional].get("status") in {"unavailable", "integrity_error"}:
            note = outcome_by_artifact[optional].get("note", "no detail")
            limitations.append(f"Optional artifact {optional} was not captured: {note}")

    verified_manifest_names: set[str] = set()
    for row in manifest_rows:
        name = row["artifact"]
        if not name or Path(name).name != name:
            errors.append(f"Manifest contains unsafe artifact name: {name!r}")
            manifest_valid = False
            continue
        if name in {"manifest.tsv", "manifest.sha256"}:
            errors.append(f"manifest.tsv lists a file the format excludes: {name}")
            manifest_valid = False
            continue
        artifact = bundle / name
        if artifact.is_symlink() or not artifact.is_file():
            errors.append(f"Manifest artifact missing or unsafe: {name}")
            manifest_valid = False
            continue
        row_verified = True
        if not re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", "")):
            errors.append(f"Manifest digest is not a sha256 value: {name}")
            manifest_valid = False
            row_verified = False
        elif sha256_file(artifact) != row["sha256"]:
            errors.append(f"Manifest checksum mismatch: {name}")
            manifest_valid = False
            row_verified = False
        if not valid_process_exit(row.get("command_exit", "")):
            errors.append(f"Manifest command exit is not a process exit status: {name}")
            manifest_valid = False
            row_verified = False
        try:
            expected_bytes = int(row.get("bytes", ""))
        except ValueError:
            errors.append(f"Manifest byte count is invalid: {name}")
            manifest_valid = False
            row_verified = False
        else:
            if expected_bytes < 0 or artifact.stat().st_size != expected_bytes:
                errors.append(f"Manifest byte count mismatch: {name}")
                manifest_valid = False
                row_verified = False
        if row_verified:
            verified_manifest_names.add(name)

    # The manifest has to cover the interpretation records too, and it has to
    # agree with command-outcomes.tsv. A manifest that silently omits an
    # artifact is the failure mode this whole bundle format exists to prevent.
    if manifest_rows:
        manifest_names = [row.get("artifact", "") for row in manifest_rows]
        duplicates = {name for name in manifest_names if manifest_names.count(name) > 1}
        for name in sorted(duplicates):
            errors.append(f"manifest.tsv lists {name} more than once.")
            manifest_valid = False

        manifest_by_name = {row.get("artifact", ""): row for row in manifest_rows}
        for record in ("command-outcomes.tsv", "collection.json", "collector-summary.md"):
            if (bundle / record).is_file() and record not in manifest_by_name:
                errors.append(f"Collection record is present but absent from manifest.tsv: {record}")
                manifest_valid = False

        for row in outcomes:
            if row.get("status") != "captured":
                continue
            name = row.get("artifact", "")
            manifest_row = manifest_by_name.get(name)
            if manifest_row is None:
                errors.append(f"command-outcomes.tsv records {name} as captured, but manifest.tsv omits it.")
                manifest_valid = False
                continue
            if manifest_row.get("command_class", "") != row.get("command_class", ""):
                errors.append(f"manifest.tsv and command-outcomes.tsv disagree on command_class for {name}.")
                manifest_valid = False
            if manifest_row.get("command_exit", "") != row.get("exit_code", ""):
                errors.append(f"manifest.tsv and command-outcomes.tsv disagree on command_exit for {name}.")
                manifest_valid = False

        present = {
            item.name
            for item in bundle.iterdir()
            if item.is_file() and item.name not in {"manifest.tsv", "manifest.sha256"}
        }
        for name in sorted(present - set(manifest_names)):
            errors.append(f"File present in the bundle but absent from manifest.tsv: {name}")
            manifest_valid = False

    anchor = bundle / "manifest.sha256"
    if anchor.is_file() and not anchor.is_symlink():
        recorded = anchor.read_text(encoding="utf-8", errors="replace").split()
        manifest_digest = sha256_file(bundle / "manifest.tsv") if (bundle / "manifest.tsv").is_file() else ""
        if not recorded or recorded[0] != manifest_digest:
            errors.append("manifest.sha256 does not match the digest of manifest.tsv in this bundle.")
            manifest_valid = False
    else:
        limitations.append(
            "No manifest.sha256 anchor is present, so the manifest itself carries no separate digest."
        )

    # A command that exited nonzero still produced an artifact, so this limits
    # how the artifact reads. It is not the same as evidence being absent, and
    # it must not be reported as an incomplete bundle.
    for row in outcomes:
        exit_code = row.get("exit_code", "")
        if row.get("status") == "captured" and exit_code not in ("", "0"):
            name = row.get("artifact", "unknown")
            command_exit_notes.append(
                f"{name} was written by a command that exited {exit_code}. "
                "Read it as the output of a failed command rather than as a complete answer."
            )

    expected_count = metadata.get("artifact_count")
    if isinstance(expected_count, int):
        actual_count = sum(
            1
            for item in bundle.iterdir()
            if item.is_file() and item.name not in {"manifest.tsv", "collection.json", "manifest.sha256"}
        )
        if expected_count != actual_count:
            limitations.append(
                f"collection.json artifact_count ({expected_count}) differs from current file count ({actual_count}); "
                "the bundle may have been updated after metadata generation."
            )
    else:
        limitations.append("collection.json artifact_count is absent or not an integer.")

    # Returned as working lists. The immutable ValidationResult is built once, in
    # finalize_validation, after every producer of limitations has run. Building
    # it here would let a later parser append a limitation to a result whose
    # status was already decided, which is how a report can say "valid" and then
    # list a problem underneath.
    if not manifest_valid or not outcomes_valid:
        # Either half of the contract failing withdraws trust from all content.
        # The manifest says the bytes are what was collected; the outcome record
        # says what those bytes are. One without the other proves nothing.
        verified_manifest_names = set()

    # One resolution of the outcome record, used everywhere downstream. Three
    # different lookups over a duplicated row produced three different answers.
    validated_outcomes = {row["artifact"]: row for row in outcomes} if outcomes_valid else {}

    return (
        DraftValidation(
            limitations,
            command_exit_notes,
            errors,
            manifest_valid,
            outcomes_valid,
            metadata_valid,
            verified_manifest_names,
            validated_outcomes,
        ),
        outcomes,
        manifest_rows,
    )


def finalize_validation(draft: "DraftValidation") -> ValidationResult:
    """Decide the status once, from the complete set of findings."""
    evidence_incomplete = bool(
        draft.errors or draft.limitations or not draft.manifest_valid or not draft.outcomes_valid
    )
    limitations = tuple(draft.limitations) + tuple(draft.command_exit_notes)
    errors = tuple(draft.errors)
    if errors:
        status = "invalid"
    elif limitations or not draft.manifest_valid:
        status = "valid_with_limitations"
    else:
        status = "valid"
    return ValidationResult(
        status, limitations, errors, draft.manifest_valid, draft.outcomes_valid, evidence_incomplete
    )


def parse_systemctl_show(
    bundle: Path,
    limitations: list[str],
    validated_outcomes: dict[str, dict[str, str]],
    trusted_names: set[str],
) -> dict[str, str]:
    outcome = validated_outcomes.get("systemctl-show.txt")
    if outcome is None or outcome.get("status") != "captured":
        limitations.append(
            "systemctl-show.txt is not recorded as captured, so no state facts were read from it."
        )
        return {}
    if outcome.get("exit_code") not in ("", "0"):
        limitations.append(
            f"State facts below come from a systemctl show that exited {outcome.get('exit_code')}. "
            "Read them as the output of a failed command."
        )
    text = trusted_artifact_text(bundle, "systemctl-show.txt", limitations, validated_outcomes, trusted_names)
    if not text:
        return {}
    facts: dict[str, str] = {}
    allowlist = {
        "Id",
        "LoadState",
        "ActiveState",
        "SubState",
        "Result",
        "MainPID",
        "ExecMainCode",
        "ExecMainStatus",
        "NRestarts",
        "Restart",
        "RestartUSec",
        "TimeoutStartUSec",
        "TimeoutStopUSec",
        "User",
        "Group",
        "WorkingDirectory",
        "FragmentPath",
        "DropInPaths",
    }
    for line in text.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in allowlist:
            facts[key] = value
    if not facts:
        limitations.append("No selected systemctl properties could be parsed from systemctl-show.txt.")
    return facts


def trusted_captured_exit(
    artifact: str,
    validated_outcomes: dict[str, dict[str, str]],
    trusted_names: set[str],
) -> str | None:
    """The exit status of a command whose artifact the contract vouches for.

    An integrity_error row can carry a nonzero exit from the command that ran,
    but that artifact was never retained. Reading it as target evidence would
    turn a collection failure into a statement about the service.
    """
    outcome = validated_outcomes.get(artifact)
    if outcome is None or outcome.get("status") != "captured":
        return None
    if artifact not in trusted_names:
        return None
    return outcome.get("exit_code", "").strip() or None


def trusted_artifact_text(
    bundle: Path,
    artifact: str,
    limitations: list[str],
    validated_outcomes: dict[str, dict[str, str]],
    trusted_names: set[str],
) -> str:
    """Read an artifact only when the bundle contract vouches for it.

    A file sitting in the directory is not evidence. It becomes evidence when
    command-outcomes.tsv records it as captured and the manifest covers it with
    a digest that verified. Reading anything else would let a file added after
    collection drive an observation, which is the failure this tool exists to
    prevent.
    """
    outcome = validated_outcomes.get(artifact)
    if outcome is None or outcome.get("status") != "captured":
        return ""
    if artifact not in trusted_names:
        limitations.append(
            f"{artifact} is recorded as captured but is not covered by a verified manifest entry, "
            "so no observation was derived from its content."
        )
        return ""
    path = bundle / artifact
    if not path.is_file() or path.is_symlink():
        return ""
    try:
        return read_text(path)
    except ValueError as exc:
        limitations.append(str(exc))
        return ""


RESTART_ABSENT = "ABSENT"
RESTART_RESET = "RESET"


def declared_restart(unit_text: str) -> str:
    """Return the Restart= value the captured unit text assigns last in [Service].

    Reads systemctl cat output, which concatenates the fragment and any drop-ins
    in load order, so the last assignment is the one the text intends. Returns
    RESTART_ABSENT when the text assigns nothing, RESTART_RESET when the last
    assignment is empty (which resets the setting), and otherwise the value.
    """
    section = ""
    value = RESTART_ABSENT
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
            continue
        if section != "[Service]" or "=" not in line:
            continue
        key, assigned = line.split("=", 1)
        if key.strip() == "Restart":
            assigned = assigned.strip()
            value = assigned or RESTART_RESET
    return value


def add_observation(
    observations: list[Observation],
    identifier: str,
    severity: str,
    statement: str,
    evidence: list[str],
    reference: str,
) -> None:
    if not any(existing.id == identifier for existing in observations):
        observations.append(Observation(identifier, severity, statement, evidence, reference))


def load_observation_inputs(
    bundle: Path,
    limitations: list[str],
    validated_outcomes: dict[str, dict[str, str]],
    trusted_names: set[str],
) -> dict[str, str]:
    """Read every artifact an observation rule may use, before validation is final.

    Reads can add limitations, so they all happen while the findings list is
    still mutable. Anything the contract does not vouch for comes back empty.
    """
    return {
        artifact: trusted_artifact_text(bundle, artifact, limitations, validated_outcomes, trusted_names)
        for artifact in ("journal.txt", "path-metadata.txt", "unit-effective.txt", "unit-verify.txt")
    }


def create_observations(
    validation: ValidationResult,
    validated_outcomes: dict[str, dict[str, str]],
    facts: dict[str, str],
    observation_inputs: dict[str, str],
    trusted_names: set[str],
) -> list[Observation]:
    observations: list[Observation] = []
    journal = observation_inputs.get("journal.txt", "").lower()
    path_metadata = observation_inputs.get("path-metadata.txt", "").lower()
    active_state = facts.get("ActiveState", "")
    restart_count = facts.get("NRestarts", "")
    health_exit = trusted_captured_exit("health.txt", validated_outcomes, trusted_names)
    unit_text_trusted = "unit-effective.txt" in trusted_names and (
        validated_outcomes.get("unit-effective.txt", {}).get("status") == "captured"
    )
    unit_text = observation_inputs.get("unit-effective.txt", "")
    verify_text = observation_inputs.get("unit-verify.txt", "")
    verify_exit = trusted_captured_exit("unit-verify.txt", validated_outcomes, trusted_names)
    effective_restart = facts.get("Restart")

    if active_state == "failed":
        add_observation(
            observations,
            "OBS-UNIT-FAILED",
            "review",
            "The unit is reported as failed in the captured state evidence. Review Result, ExecMain fields, and the bounded journal window before considering a recovery action.",
            ["systemctl-show.txt", "systemctl-status.txt"],
            "docs/review_guide.md#failed-unit",
        )

    if active_state == "active" and health_exit not in (None, "0"):
        add_observation(
            observations,
            "OBS-ACTIVE-HEALTH-FAIL",
            "review",
            "The unit appears active while the supplied local health request did not meet its success condition. Review application, dependency, and request-path evidence.",
            ["systemctl-show.txt", "health.txt", "health.stderr"],
            "docs/review_guide.md#active-but-unhealthy",
        )

    if any(token in journal for token in ("invalid configuration", "config validation", "missing required setting")):
        add_observation(
            observations,
            "OBS-CONFIG-LIKE",
            "review",
            "The journal contains a configuration-like signal. Confirm the current configuration against a known-good baseline before considering service recovery.",
            ["journal.txt", "systemctl-show.txt"],
            "docs/review_guide.md#configuration-signals",
        )

    if any(token in journal or token in path_metadata for token in ("permission denied", "access denied", "-rw------- root root")):
        add_observation(
            observations,
            "OBS-ACCESS-LIKE",
            "review",
            "The captured evidence contains an access-related signal. Review the configured service identity and narrow target-path metadata; do not broaden permissions as a shortcut.",
            ["journal.txt", "path-metadata.txt", "systemctl-show.txt"],
            "docs/review_guide.md#access-signals",
        )

    if any(token in journal for token in ("connection refused", "dependency is unavailable", "dependency connection")):
        add_observation(
            observations,
            "OBS-DEPENDENCY-LIKE",
            "review",
            "The captured evidence contains a dependency/connectivity-like signal. Review dependency health before retrying the dependent service.",
            ["journal.txt", "health.stderr"],
            "docs/review_guide.md#dependency-signals",
        )

    restart_count_is_positive = restart_count.isdigit() and int(restart_count) > 0
    restart_signal_in_journal = (
        "start request repeated too quickly" in journal
        or "start-limit" in journal
    )
    if restart_count_is_positive or restart_signal_in_journal:
        add_observation(
            observations,
            "OBS-RESTART-CONTEXT",
            "caution",
            "Restart-related context is present. Do not clear/reset/retry solely from this report; review causal evidence and the stop conditions in docs/review_guide.md.",
            ["systemctl-show.txt", "journal.txt"],
            "docs/review_guide.md#restart-context",
        )

    if verify_text.strip():
        add_observation(
            observations,
            "OBS-UNIT-VERIFY-DIAGNOSTICS",
            "review",
            f"systemd-analyze verify produced diagnostic output (exit {verify_exit or 'unknown'}). Exit 0 with diagnostics does not mean the unit is clean; a directive named in the output may have been ignored.",
            ["unit-verify.txt", "command-outcomes.tsv"],
            "docs/review_guide.md#configuration-signals",
        )

    # The whole comparison lives inside the trust check. Nothing about a unit
    # file is computed unless that file was captured and its manifest row
    # verified, because an absent file would otherwise read as "no Restart
    # assignment", which is a statement about a file nobody opened.
    if unit_text_trusted and effective_restart is not None:
        declared = declared_restart(unit_text)
        # An absent assignment and an explicit reset both mean the unit text asks
        # for Restart=no. Comparing them is the point: a manager still holding an
        # older value is the stale-configuration case this rule exists to surface.
        expected_effective = "no" if declared in {RESTART_ABSENT, RESTART_RESET} else declared
        declared_description = {
            RESTART_ABSENT: "no Restart assignment, which means Restart=no",
            RESTART_RESET: "an empty Restart assignment, which resets it to Restart=no",
        }.get(declared, f"Restart={declared}")
        verify_captured = "unit-verify.txt" in trusted_names and (
            validated_outcomes.get("unit-verify.txt", {}).get("status") == "captured"
        )
        if expected_effective != effective_restart:
            evidence = ["unit-effective.txt", "systemctl-show.txt"]
            if verify_captured:
                evidence.append("unit-verify.txt")
            add_observation(
                observations,
                "OBS-RESTART-DECLARED-DIFFERS",
                "review",
                f"The captured unit text carries {declared_description}, but the manager reports Restart={effective_restart}. The declared restart policy may not be the one in effect; compare the unit text, drop-ins, and any verifier output before relying on automatic restart.",
                evidence,
                "docs/review_guide.md#configuration-signals",
            )

    if validation.evidence_incomplete:
        add_observation(
            observations,
            "OBS-BUNDLE-LIMITED",
            "caution",
            "Something bounds what this bundle can answer: a validation error, an artifact that was not captured, or a limitation on what was collected. Read the validation section to see which. Do not treat an absent artifact or a bounded capture as evidence of service health.",
            ["collection.json", "manifest.tsv", "command-outcomes.tsv"],
            "docs/review_guide.md#limited-bundle",
        )

    if not observations:
        add_observation(
            observations,
            "OBS-NO-TRIGGERED-RULE",
            "information",
            "No conservative observation rule was triggered by the selected artifacts. This is not proof that the service or environment is healthy; review the bundle in context.",
            ["systemctl-show.txt", "command-outcomes.tsv"],
            "docs/review_guide.md#no-rule-triggered",
        )

    # An observation may cite only evidence the contract vouches for. A
    # structural observation may additionally name the records it is
    # criticizing, because those records are its subject.
    collection_records = {
        "manifest.tsv",
        "manifest.sha256",
        "command-outcomes.tsv",
        "collection.json",
        "collector-summary.md",
    }
    filtered: list[Observation] = []
    for observation in observations:
        allowed = trusted_names | collection_records
        kept = [reference for reference in observation.evidence if reference in allowed]
        filtered.append(replace(observation, evidence=kept or ["command-outcomes.tsv"]))
    return filtered


def markdown_cell(value: Any) -> str:
    """Render a bundle-supplied value inside a table cell without breaking it.

    Values come from a file this tool did not write, so a pipe, backtick, or
    newline in one of them must not be able to reshape the report.
    """
    text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    text = text.replace("|", "\\|").replace("`", "'")
    return text


def render_markdown(
    metadata: dict[str, Any],
    validation: ValidationResult,
    facts: dict[str, str],
    observations: list[Observation],
    bundle: Path,
    metadata_trusted: bool = True,
) -> str:
    manifest_path = bundle / "manifest.tsv"
    manifest_digest = sha256_file(manifest_path) if manifest_path.is_file() else "not present"

    def identity_value(value: Any) -> str:
        rendered = markdown_cell(value)
        return rendered if metadata_trusted else f"{rendered} (unverified)"

    def identity(field: str, default: str = "unknown") -> str:
        """Report identity comes from collection.json, which is itself evidence.

        When that record failed verification, the values are still shown, since
        hiding them helps nobody, but they are labelled rather than presented as
        established fact.
        """
        return identity_value(metadata.get(field, default))
    lines = [
        "# Evidence Bundle Human-Review Report",
        "",
        "## Scope",
        "",
        f"| Field | Value |",
        f"|---|---|",
        f"| **Unit** | `{identity('unit')}` |",
        f"| **Collected at (UTC)** | `{identity('collected_at_utc')}` |",
        f"| **Collector version** | `{identity('collector_version')}` |",
        f"| **Analyzer version** | `{ANALYZER_VERSION}` |",
        f"| **Collection mode** | `{identity('mode')}` |",
        f"| **Journal access** | `{identity_value((metadata.get('collection_context') or {}).get('journal_access', 'not recorded'))}` |",
        f"| **Computed manifest.tsv sha256** | `{manifest_digest}` |",
        f"| **Validation status** | **{validation.status}** |",
        "",
        "> This report is evidence-linked and observational, with bounded review guidance. It identifies no root cause and authorizes no restart, reload, reset, or other state-changing action.",
        "",
        "## Parsed State Facts",
        "",
        "| Property | Captured value |",
        "|---|---|",
    ]
    for key in (
        "LoadState",
        "ActiveState",
        "SubState",
        "Result",
        "MainPID",
        "ExecMainCode",
        "ExecMainStatus",
        "NRestarts",
        "Restart",
        "RestartUSec",
        "User",
        "Group",
        "WorkingDirectory",
        "FragmentPath",
    ):
        lines.append(f"| `{key}` | `{markdown_cell(facts.get(key, 'not captured'))}` |")

    lines.extend(["", "## Validation", ""])
    if validation.errors:
        lines.append("### Errors")
        lines.append("")
        for error in validation.errors:
            lines.append(f"- {error}")
        lines.append("")
    if validation.limitations:
        lines.append("### Limitations")
        lines.append("")
        for limitation in validation.limitations:
            lines.append(f"- {limitation}")
        lines.append("")
    if not validation.errors and not validation.limitations:
        lines.append("The bundle satisfied the currently implemented validation checks.")
        lines.append("")

    lines.extend(["## Observations Requiring Human Review", ""])
    for observation in observations:
        lines.extend(
            [
                f"### {observation.id} ({observation.severity})",
                "",
                observation.statement,
                "",
                f"**Evidence:** {', '.join(f'`{item}`' for item in observation.evidence)}  ",
                f"**Next step:** {observation.playbook_reference}",
                "",
            ]
        )

    lines.extend(
        [
            "## Next Review Step",
            "",
            "Work the referenced section of docs/review_guide.md in the repository that produced this report. Preserve the bundle, verify scope and safety, and treat this report as review input rather than an execution instruction.",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(output_parent: Path, metadata: dict[str, Any], analysis: dict[str, Any], markdown: str) -> Path:
    if not output_parent.exists():
        output_parent.mkdir(parents=True)
        output_parent.chmod(0o700)
    unit_stem = str(metadata.get("unit", "unknown.service")).removesuffix(".service")
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]", "_", unit_stem)
    # mkdtemp rather than a timestamp name: two analyses of the same unit in the
    # same second would otherwise collide, and a test runner does exactly that.
    report_dir = Path(mkdtemp(prefix=f"report-{utc_now_compact()}-{safe_stem}-", dir=output_parent))
    report_dir.chmod(0o700)
    json_path = report_dir / "analysis.json"
    md_path = report_dir / "analysis.md"
    validation_path = report_dir / "validation.txt"
    json_path.write_text(json.dumps(analysis, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    validation_path.write_text(
        "This report is generated from a supplied local evidence bundle. "
        "It did not run system commands, request network resources, or perform state changes.\n",
        encoding="utf-8",
    )
    for artifact in (json_path, md_path, validation_path):
        artifact.chmod(0o600)
    return report_dir


def main() -> int:
    args = parse_args()
    bundle_argument = Path(args.bundle).expanduser()
    if bundle_argument.is_symlink():
        print("Error: --bundle must not be a symlink.", file=sys.stderr)
        return 2
    bundle = bundle_argument.resolve()
    output_parent = Path(args.output_dir).expanduser().resolve()
    if not bundle.is_dir():
        print("Error: --bundle must be an existing non-symlink directory.", file=sys.stderr)
        return 2
    # Writing reports into the bundle would add files the manifest does not
    # cover, so a later verification of that bundle would fail on this tool's
    # own output. A directory alongside or above the bundle is fine.
    if bundle == output_parent or bundle in output_parent.parents:
        print(
            "Error: --output-dir must not be the bundle directory or a directory inside it.",
            file=sys.stderr,
        )
        return 2

    try:
        metadata = load_json(bundle / "collection.json")
        draft, outcomes, _manifest_rows = validate_bundle(bundle, metadata)
        facts = parse_systemctl_show(
            bundle, draft.limitations, draft.validated_outcomes, draft.verified_manifest_names
        )
        observation_inputs = load_observation_inputs(
            bundle, draft.limitations, draft.validated_outcomes, draft.verified_manifest_names
        )
        validation = finalize_validation(draft)
        observations = create_observations(
            validation,
            draft.validated_outcomes,
            facts,
            observation_inputs,
            draft.verified_manifest_names,
        )
        # Trust needs both halves: the record's bytes verified, and the record
        # says something the contract accepts. A correctly hashed file full of
        # malformed metadata is authentic and still unusable as fact.
        metadata_trusted = (
            draft.metadata_valid and "collection.json" in draft.verified_manifest_names
        )
        analysis = {
            "schema_version": SCHEMA_VERSION,
            "analysis_version": ANALYZER_VERSION,
            "source_bundle": {
                "unit": metadata.get("unit", "unknown"),
                "collected_at_utc": metadata.get("collected_at_utc", "unknown"),
                "manifest_valid": validation.manifest_valid,
                "outcomes_valid": validation.outcomes_valid,
                # False means the collection record is not presented as fact:
                # either its manifest row did not verify within an otherwise
                # valid contract, or its contents failed metadata validation.
                # The values are kept, not trusted.
                "metadata_trusted": metadata_trusted,
            },
            "validation": asdict(validation),
            "facts": facts,
            "observations": [asdict(observation) for observation in observations],
        }
        markdown = render_markdown(metadata, validation, facts, observations, bundle, metadata_trusted)
        report_dir = write_reports(output_parent, metadata, analysis, markdown)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Analysis error: {exc}", file=sys.stderr)
        return 3

    print(f"Human-review report created: {report_dir}")
    print("This analyzer is observational and did not perform system or network actions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
