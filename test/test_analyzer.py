#!/usr/bin/env python3
"""Deterministic tests for the local evidence-bundle analyzer."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ANALYZER = PROJECT_ROOT / "analyze_evidence_bundle.py"
FIXTURES = PROJECT_ROOT / "test" / "fixtures" / "generated"


def setUpModule():
    """Regenerate the fixtures so this module passes on a fresh checkout.

    The fixtures come from the collector's own --fixture mode and are not
    committed. Without this, `python -m unittest discover -s test` fails on
    every clean clone, which is exactly how the CI python job runs it.
    """
    subprocess.run(["bash", str(PROJECT_ROOT / "test" / "generate_fixtures.sh")],
                   check=True, stdout=subprocess.DEVNULL)


class AnalyzerFixtureTests(unittest.TestCase):
    maxDiff = None

    def analyze_fixture(self, fixture_name: str) -> dict:
        bundle = FIXTURES / fixture_name
        self.assertTrue(bundle.is_dir(), f"fixture missing: {bundle}")
        with tempfile.TemporaryDirectory() as temp_dir:
            completed = subprocess.run(
                [sys.executable, str(ANALYZER), "--bundle", str(bundle), "--output-dir", temp_dir],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            reports = sorted(Path(temp_dir).glob("report-*"))
            self.assertEqual(len(reports), 1, completed.stdout)
            return json.loads((reports[0] / "analysis.json").read_text(encoding="utf-8"))

    def observation_ids(self, fixture_name: str) -> set[str]:
        analysis = self.analyze_fixture(fixture_name)
        return {entry["id"] for entry in analysis["observations"]}

    def test_healthy_fixture_has_no_problem_observation(self) -> None:
        analysis = self.analyze_fixture("healthy")
        self.assertEqual(analysis["validation"]["status"], "valid")
        self.assertEqual(self.observation_ids("healthy"), {"OBS-NO-TRIGGERED-RULE"})

    def test_configuration_failure_is_observed_conservatively(self) -> None:
        observation_ids = self.observation_ids("failed_config")
        self.assertIn("OBS-UNIT-FAILED", observation_ids)
        self.assertIn("OBS-CONFIG-LIKE", observation_ids)
        self.assertNotIn("OBS-DEPENDENCY-LIKE", observation_ids)

    def test_permission_failure_includes_access_observation(self) -> None:
        observation_ids = self.observation_ids("permission_failure")
        self.assertIn("OBS-UNIT-FAILED", observation_ids)
        self.assertIn("OBS-ACCESS-LIKE", observation_ids)

    def test_dependency_failure_distinguishes_active_process_from_health(self) -> None:
        observation_ids = self.observation_ids("dependency_failure")
        self.assertIn("OBS-ACTIVE-HEALTH-FAIL", observation_ids)
        self.assertIn("OBS-DEPENDENCY-LIKE", observation_ids)
        self.assertNotIn("OBS-UNIT-FAILED", observation_ids)

    def test_incomplete_bundle_is_labeled_as_a_limitation(self) -> None:
        analysis = self.analyze_fixture("incomplete_bundle")
        observation_ids = {entry["id"] for entry in analysis["observations"]}
        self.assertEqual(analysis["validation"]["status"], "valid_with_limitations")
        self.assertIn("OBS-BUNDLE-LIMITED", observation_ids)

    def test_ignored_directive_reports_verify_diagnostics_and_restart_mismatch(self) -> None:
        observation_ids = self.observation_ids("ignored_directive")
        self.assertIn("OBS-UNIT-FAILED", observation_ids)
        self.assertIn("OBS-UNIT-VERIFY-DIAGNOSTICS", observation_ids)
        self.assertIn("OBS-RESTART-DECLARED-DIFFERS", observation_ids)
        self.assertNotIn("OBS-RESTART-CONTEXT", observation_ids)

    def test_clean_verify_and_matching_restart_add_no_observation(self) -> None:
        for fixture in ("healthy", "failed_config", "permission_failure", "dependency_failure", "incomplete_bundle"):
            with self.subTest(fixture=fixture):
                observation_ids = self.observation_ids(fixture)
                self.assertNotIn("OBS-UNIT-VERIFY-DIAGNOSTICS", observation_ids)
                self.assertNotIn("OBS-RESTART-DECLARED-DIFFERS", observation_ids)

    def test_verify_diagnostics_are_reported_at_exit_zero(self) -> None:
        outcomes = (FIXTURES / "ignored_directive" / "command-outcomes.tsv").read_text(encoding="utf-8")
        row = next(line.split("\t") for line in outcomes.splitlines() if line.startswith("unit-verify.txt\t"))
        self.assertEqual(row[3], "0")
        self.assertIn("1 diagnostic line(s)", row[4])

    def test_declared_restart_follows_drop_in_order_and_sections(self) -> None:
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from analyze_evidence_bundle import declared_restart
        finally:
            sys.path.pop(0)
        base = "[Unit]\nDescription=x\n\n[Service]\nExecStart=/bin/true\nRestart=on-failure\n"
        self.assertEqual(declared_restart(base), "on-failure")
        drop_in = base + "# /etc/systemd/system/x.service.d/override.conf\n[Service]\nRestart=always\n"
        self.assertEqual(declared_restart(drop_in), "always")
        reset = base + "[Service]\nRestart=\n"
        self.assertEqual(declared_restart(reset), "RESET")
        wrong_section = "[Unit]\nRestart=always\n\n[Service]\nExecStart=/bin/true\n"
        self.assertEqual(declared_restart(wrong_section), "ABSENT")
        # An absent assignment and an explicit reset are different states and
        # must not collapse into one value.
        self.assertNotEqual(declared_restart(reset), declared_restart(wrong_section))

    # --- Adversarial bundle tests --------------------------------------------
    # Each one attacks a claim the README makes about the bundle format.

    def analyze_mutated(self, fixture_name: str, mutate) -> dict:
        """Copy a fixture, let `mutate` edit it, analyze, return analysis.json."""
        with tempfile.TemporaryDirectory() as temp_dir:
            copied_bundle = Path(temp_dir) / "mutated"
            shutil.copytree(FIXTURES / fixture_name, copied_bundle)
            mutate(copied_bundle)
            report_parent = Path(temp_dir) / "reports"
            completed = subprocess.run(
                [sys.executable, str(ANALYZER), "--bundle", str(copied_bundle), "--output-dir", str(report_parent)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = next(report_parent.glob("report-*"))
            return json.loads((report / "analysis.json").read_text(encoding="utf-8"))

    def assert_invalid_with(self, analysis: dict, needle: str) -> None:
        self.assertEqual(analysis["validation"]["status"], "invalid", analysis["validation"])
        joined = " | ".join(analysis["validation"]["errors"]).lower()
        self.assertIn(needle.lower(), joined, joined)

    def test_manifest_covers_the_collection_records(self) -> None:
        manifest = (FIXTURES / "healthy" / "manifest.tsv").read_text(encoding="utf-8")
        for record in ("command-outcomes.tsv", "collection.json", "collector-summary.md"):
            self.assertIn(record, manifest)

    def test_manifest_omitting_a_captured_artifact_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            manifest = bundle / "manifest.tsv"
            kept = [
                line for line in manifest.read_text(encoding="utf-8").splitlines()
                if not line.startswith("journal.txt\t")
            ]
            manifest.write_text("\n".join(kept) + "\n", encoding="utf-8")
            self.rewrite_anchor(bundle)

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "omits it")

    def test_duplicate_manifest_row_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            manifest = bundle / "manifest.tsv"
            lines = manifest.read_text(encoding="utf-8").splitlines()
            duplicate = next(line for line in lines if line.startswith("journal.txt\t"))
            manifest.write_text("\n".join(lines + [duplicate]) + "\n", encoding="utf-8")
            self.rewrite_anchor(bundle)

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "more than once")

    def test_unmanifested_file_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            (bundle / "extra-evidence.txt").write_text("added later\n", encoding="utf-8")

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "absent from manifest")

    def test_modified_collection_metadata_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            path = bundle / "collection.json"
            metadata = json.loads(path.read_text(encoding="utf-8"))
            metadata["collection_context"]["journal_access"] = "full_as_root"
            path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "checksum mismatch")

    def test_modified_outcome_record_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            path = bundle / "command-outcomes.tsv"
            path.write_text(path.read_text(encoding="utf-8").replace("captured", "captured "), encoding="utf-8")

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "checksum mismatch")

    def test_command_exit_disagreement_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            manifest = bundle / "manifest.tsv"
            rows = []
            for line in manifest.read_text(encoding="utf-8").splitlines():
                if line.startswith("journal.txt\t"):
                    fields = line.split("\t")
                    fields[5] = "7"
                    line = "\t".join(fields)
                rows.append(line)
            manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
            self.rewrite_anchor(bundle)

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "disagree on command_exit")

    def test_altered_manifest_anchor_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            (bundle / "manifest.sha256").write_text("0" * 64 + "  manifest.tsv\n", encoding="utf-8")

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "manifest.sha256")

    def test_nonzero_command_exit_becomes_a_limitation(self) -> None:
        analysis = self.analyze_fixture("failed_config")
        limitations = " | ".join(analysis["validation"]["limitations"]).lower()
        self.assertIn("exited", limitations, limitations)

    def test_report_cites_no_evidence_file_that_was_not_captured(self) -> None:
        for fixture in ("healthy", "failed_config", "permission_failure",
                        "dependency_failure", "incomplete_bundle", "ignored_directive"):
            with self.subTest(fixture=fixture):
                analysis = self.analyze_fixture(fixture)
                outcomes = (FIXTURES / fixture / "command-outcomes.tsv").read_text(encoding="utf-8")
                captured = {
                    line.split("\t")[0]
                    for line in outcomes.splitlines()[1:]
                    if line.split("\t")[2] == "captured"
                }
                for observation in analysis["observations"]:
                    for reference in observation.get("evidence", []):
                        if reference.endswith((".txt", ".tsv", ".json", ".md", ".stderr")):
                            records = {"command-outcomes.tsv", "collection.json", "collector-summary.md",
                                       "manifest.tsv", "manifest.sha256"}
                            self.assertIn(reference, captured | records,
                                          f"{observation['id']} cites uncaptured {reference}")

    def test_schema_1_2_records_inputs_and_host_context(self) -> None:
        metadata = json.loads((FIXTURES / "healthy" / "collection.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["schema_version"], "1.2")
        self.assertIn("inputs", metadata)
        self.assertIn("systemd_version", metadata["collection_context"])
        self.assertIn("journal_until_utc", metadata)

    def rewrite_anchor(self, bundle: Path) -> None:
        """Keep manifest.sha256 consistent so a test attacks only its own target."""
        import hashlib

        digest = hashlib.sha256((bundle / "manifest.tsv").read_bytes()).hexdigest()
        (bundle / "manifest.sha256").write_text(f"{digest}  manifest.tsv\n", encoding="utf-8")

    def test_header_only_manifest_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            manifest = bundle / "manifest.tsv"
            header = manifest.read_text(encoding="utf-8").splitlines()[0]
            manifest.write_text(header + "\n", encoding="utf-8")
            self.rewrite_anchor(bundle)

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "no artifact records")

    def test_malformed_manifest_row_fails_cleanly(self) -> None:
        def mutate(bundle: Path) -> None:
            manifest = bundle / "manifest.tsv"
            manifest.write_text(
                manifest.read_text(encoding="utf-8") + "truncated-row\tonly-two-fields\n",
                encoding="utf-8",
            )
            self.rewrite_anchor(bundle)

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "fields; expected")

    def test_duplicate_outcome_row_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            outcomes = bundle / "command-outcomes.tsv"
            lines = outcomes.read_text(encoding="utf-8").splitlines()
            duplicate = next(line for line in lines if line.startswith("journal.txt\t"))
            outcomes.write_text("\n".join(lines + [duplicate]) + "\n", encoding="utf-8")

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "more than once")

    def test_unrecognized_outcome_status_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            outcomes = bundle / "command-outcomes.tsv"
            outcomes.write_text(
                outcomes.read_text(encoding="utf-8").replace("\tcaptured\t", "\tsuccessish\t", 1),
                encoding="utf-8",
            )

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "unrecognized status")

    def test_only_the_implemented_schema_is_accepted(self) -> None:
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from analyze_evidence_bundle import SUPPORTED_SCHEMA_VERSIONS
        finally:
            sys.path.pop(0)
        # Claiming support for a schema whose bundles this validator rejects
        # would be a promise the code breaks.
        self.assertEqual(SUPPORTED_SCHEMA_VERSIONS, ("1.2",))

    def test_validation_status_accounts_for_every_limitation(self) -> None:
        """No limitation may be added after the status has been decided."""
        analysis = self.analyze_fixture("failed_config")
        validation = analysis["validation"]
        if validation["limitations"] or validation["errors"]:
            self.assertNotEqual(validation["status"], "valid", validation)

    def test_two_analyses_in_the_same_second_do_not_collide(self) -> None:
        bundle = FIXTURES / "healthy"
        with tempfile.TemporaryDirectory() as temp_dir:
            for _ in range(2):
                completed = subprocess.run(
                    [sys.executable, str(ANALYZER), "--bundle", str(bundle), "--output-dir", temp_dir],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(len(list(Path(temp_dir).glob("report-*"))), 2)

    def test_existing_output_parent_keeps_its_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_parent = Path(temp_dir) / "reports"
            output_parent.mkdir(mode=0o755)
            output_parent.chmod(0o755)
            subprocess.run(
                [sys.executable, str(ANALYZER), "--bundle", str(FIXTURES / "healthy"),
                 "--output-dir", str(output_parent)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(oct(output_parent.stat().st_mode & 0o777), oct(0o755))

    def test_reset_and_absent_restart_are_compared_against_the_manager(self) -> None:
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from analyze_evidence_bundle import RESTART_ABSENT, RESTART_RESET, declared_restart
        finally:
            sys.path.pop(0)
        reset_text = "[Service]\nExecStart=/bin/true\nRestart=on-failure\n[Service]\nRestart=\n"
        self.assertEqual(declared_restart(reset_text), RESTART_RESET)
        self.assertEqual(declared_restart("[Service]\nExecStart=/bin/true\n"), RESTART_ABSENT)

    def test_markdown_values_from_a_bundle_are_escaped(self) -> None:
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from analyze_evidence_bundle import markdown_cell
        finally:
            sys.path.pop(0)
        self.assertNotIn("|", markdown_cell("a|b").replace("\\|", ""))
        self.assertNotIn("\n", markdown_cell("a\nb"))
        self.assertNotIn("`", markdown_cell("a`b"))

    def test_no_observation_is_derived_from_an_untrusted_artifact(self) -> None:
        """A file the outcome record calls skipped must not drive an observation."""
        def mutate(bundle: Path) -> None:
            outcomes = bundle / "command-outcomes.tsv"
            rewritten = outcomes.read_text(encoding="utf-8").replace(
                "journal.txt\tjournalctl_unit\tcaptured", "journal.txt\tjournalctl_unit\tskipped"
            )
            outcomes.write_text(rewritten, encoding="utf-8")
            (bundle / "journal.txt").write_text(
                "permission denied while opening configuration\n", encoding="utf-8"
            )

        analysis = self.analyze_mutated("healthy", mutate)
        observation_ids = {entry["id"] for entry in analysis["observations"]}
        self.assertNotIn("OBS-ACCESS-LIKE", observation_ids)
        self.assertNotIn("OBS-CONFIG-LIKE", observation_ids)

    def test_manifest_valid_is_false_when_the_manifest_cannot_be_read(self) -> None:
        for description, mutate in (
            ("missing", lambda bundle: (bundle / "manifest.tsv").unlink()),
            ("malformed header", lambda bundle: (bundle / "manifest.tsv").write_text("nonsense\n", encoding="utf-8")),
            ("header only", lambda bundle: (bundle / "manifest.tsv").write_text(
                "artifact\tsha256\tbytes\tcollected_at_utc\tcommand_class\tcommand_exit\n", encoding="utf-8")),
        ):
            with self.subTest(case=description):
                analysis = self.analyze_mutated("healthy", mutate)
                self.assertFalse(analysis["source_bundle"]["manifest_valid"], analysis["validation"])
                self.assertEqual(analysis["validation"]["status"], "invalid")

    def test_content_is_ignored_once_the_manifest_is_in_question(self) -> None:
        """An invalid manifest means no artifact content is trusted at all."""
        def mutate(bundle: Path) -> None:
            (bundle / "journal.txt").write_text(
                "permission denied while opening configuration\n", encoding="utf-8"
            )

        analysis = self.analyze_mutated("healthy", mutate)
        observation_ids = {entry["id"] for entry in analysis["observations"]}
        self.assertEqual(analysis["validation"]["status"], "invalid")
        self.assertEqual(observation_ids, {"OBS-BUNDLE-LIMITED"})

    def test_manifest_digest_shape_is_enforced(self) -> None:
        def mutate(bundle: Path) -> None:
            manifest = bundle / "manifest.tsv"
            rows = []
            for line in manifest.read_text(encoding="utf-8").splitlines():
                if line.startswith("journal.txt\t"):
                    fields = line.split("\t")
                    fields[1] = "not-a-digest"
                    line = "\t".join(fields)
                rows.append(line)
            manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
            self.rewrite_anchor(bundle)

        self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "not a sha256")

    # --- Trust-boundary tests ------------------------------------------------
    # These do not merely prove that a broken bundle is called invalid. They
    # prove that a broken bundle cannot authorize a statement about the service.

    def rewrite_outcomes(self, bundle: Path, rows: list[str]) -> None:
        """Replace the outcome record and keep the manifest consistent with it."""
        import hashlib

        outcomes_path = bundle / "command-outcomes.tsv"
        text = "\n".join(rows) + "\n"
        outcomes_path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifest = bundle / "manifest.tsv"
        updated = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.startswith("command-outcomes.tsv\t"):
                fields = line.split("\t")
                fields[1] = digest
                fields[2] = str(len(text.encode("utf-8")))
                line = "\t".join(fields)
            updated.append(line)
        manifest.write_text("\n".join(updated) + "\n", encoding="utf-8")
        self.rewrite_anchor(bundle)

    def semantic_observation_ids(self, analysis: dict) -> set[str]:
        structural = {"OBS-BUNDLE-LIMITED", "OBS-NO-TRIGGERED-RULE"}
        return {entry["id"] for entry in analysis["observations"]} - structural

    def test_duplicate_outcome_rows_disable_semantic_analysis(self) -> None:
        """An internally consistent bundle with a duplicated outcome row."""
        def mutate(bundle: Path) -> None:
            rows = (bundle / "command-outcomes.tsv").read_text(encoding="utf-8").splitlines()
            duplicate = next(line for line in rows if line.startswith("journal.txt\t"))
            self.rewrite_outcomes(bundle, rows + [duplicate])

        analysis = self.analyze_mutated("failed_config", mutate)
        self.assertEqual(analysis["validation"]["status"], "invalid")
        self.assertEqual(self.semantic_observation_ids(analysis), set())

    def test_unrecognized_outcome_status_disables_semantic_analysis(self) -> None:
        def mutate(bundle: Path) -> None:
            rows = []
            for line in (bundle / "command-outcomes.tsv").read_text(encoding="utf-8").splitlines():
                if line.startswith("journal.txt\t"):
                    line = line.replace("\tcaptured\t", "\tsuccessish\t")
                rows.append(line)
            self.rewrite_outcomes(bundle, rows)

        analysis = self.analyze_mutated("failed_config", mutate)
        self.assertEqual(analysis["validation"]["status"], "invalid")
        self.assertEqual(self.semantic_observation_ids(analysis), set())

    def test_out_of_range_exit_status_is_rejected(self) -> None:
        def mutate(bundle: Path) -> None:
            rows = []
            for line in (bundle / "command-outcomes.tsv").read_text(encoding="utf-8").splitlines():
                if line.startswith("journal.txt\t"):
                    fields = line.split("\t")
                    fields[3] = "999"
                    line = "\t".join(fields)
                rows.append(line)
            self.rewrite_outcomes(bundle, rows)

        self.assert_invalid_with(
            self.analyze_mutated("failed_config", mutate), "which that status does not permit"
        )

    def health_outcome_variants(self) -> tuple[tuple[str, str], ...]:
        # Each pair is a status an outcome row may legitimately carry with an
        # exit status that status permits, so what the analyzer refuses here is
        # the status itself and not a malformed row.
        return (("integrity_error", "22"), ("unavailable", ""), ("skipped", ""))

    def test_untrusted_health_evidence_cannot_report_a_failing_probe(self) -> None:
        """Only a captured, manifest-verified health artifact is target evidence."""
        for status, exit_code in self.health_outcome_variants():
            with self.subTest(status=status):
                def mutate(bundle: Path, status=status, exit_code=exit_code) -> None:
                    rows = []
                    for line in (bundle / "command-outcomes.tsv").read_text(encoding="utf-8").splitlines():
                        if line.startswith("health.txt\t"):
                            fields = line.split("\t")
                            fields[2] = status
                            fields[3] = exit_code
                            line = "\t".join(fields)
                        rows.append(line)
                    manifest = bundle / "manifest.tsv"
                    kept = [
                        line for line in manifest.read_text(encoding="utf-8").splitlines()
                        if not line.startswith("health.txt\t")
                    ]
                    manifest.write_text("\n".join(kept) + "\n", encoding="utf-8")
                    (bundle / "health.txt").unlink()
                    self.rewrite_outcomes(bundle, rows)

                analysis = self.analyze_mutated("dependency_failure", mutate)
                self.assertNotIn("OBS-ACTIVE-HEALTH-FAIL", {entry["id"] for entry in analysis["observations"]})

    def test_unmanifested_health_evidence_cannot_report_a_failing_probe(self) -> None:
        def mutate(bundle: Path) -> None:
            manifest = bundle / "manifest.tsv"
            kept = [
                line for line in manifest.read_text(encoding="utf-8").splitlines()
                if not line.startswith("health.txt\t")
            ]
            manifest.write_text("\n".join(kept) + "\n", encoding="utf-8")
            self.rewrite_anchor(bundle)

        analysis = self.analyze_mutated("dependency_failure", mutate)
        self.assertNotIn("OBS-ACTIVE-HEALTH-FAIL", {entry["id"] for entry in analysis["observations"]})

    def test_missing_unit_text_does_not_become_restart_no(self) -> None:
        """An unavailable unit file must not be read as 'no Restart assignment'."""
        for description, mutate in (
            ("skipped", self.skip_unit_effective),
            ("deleted", self.remove_unit_effective),
        ):
            with self.subTest(case=description):
                analysis = self.analyze_mutated("ignored_directive", mutate)
                self.assertNotIn(
                    "OBS-RESTART-DECLARED-DIFFERS",
                    {entry["id"] for entry in analysis["observations"]},
                )

    def skip_unit_effective(self, bundle: Path) -> None:
        rows = []
        for line in (bundle / "command-outcomes.tsv").read_text(encoding="utf-8").splitlines():
            if line.startswith("unit-effective.txt\t"):
                line = line.replace("\tcaptured\t", "\tskipped\t")
            rows.append(line)
        manifest = bundle / "manifest.tsv"
        kept = [
            line for line in manifest.read_text(encoding="utf-8").splitlines()
            if not line.startswith("unit-effective.txt\t")
        ]
        manifest.write_text("\n".join(kept) + "\n", encoding="utf-8")
        (bundle / "unit-effective.txt").unlink()
        self.rewrite_outcomes(bundle, rows)

    def remove_unit_effective(self, bundle: Path) -> None:
        (bundle / "unit-effective.txt").unlink()

    def test_malformed_nested_metadata_fails_cleanly(self) -> None:
        def mutate(bundle: Path) -> None:
            path = bundle / "collection.json"
            metadata = json.loads(path.read_text(encoding="utf-8"))
            metadata["collection_context"] = "malformed"
            path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

        analysis = self.analyze_mutated("healthy", mutate)
        self.assertEqual(analysis["validation"]["status"], "invalid")

    def test_unverified_metadata_is_labelled_in_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = Path(temp_dir) / "mutated"
            shutil.copytree(FIXTURES / "healthy", bundle)
            metadata = json.loads((bundle / "collection.json").read_text(encoding="utf-8"))
            metadata["unit"] = "entirely-different.service"
            (bundle / "collection.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            report_parent = Path(temp_dir) / "reports"
            subprocess.run(
                [sys.executable, str(ANALYZER), "--bundle", str(bundle), "--output-dir", str(report_parent)],
                check=True, capture_output=True, text=True,
            )
            report = next(report_parent.glob("report-*"))
            markdown = (report / "analysis.md").read_text(encoding="utf-8")
            self.assertIn("(unverified)", markdown)

    def test_committed_examples_still_analyze_as_documented(self) -> None:
        """The examples in the repository are output, not decoration."""
        expected = {
            "evidence_bundle_failed_config": {"OBS-UNIT-FAILED", "OBS-CONFIG-LIKE"},
            "evidence_bundle_ignored_directive": {
                "OBS-UNIT-FAILED",
                "OBS-UNIT-VERIFY-DIAGNOSTICS",
                "OBS-RESTART-DECLARED-DIFFERS",
            },
        }
        for name, observation_ids in expected.items():
            with self.subTest(example=name):
                bundle = PROJECT_ROOT / "examples" / name
                self.assertTrue(bundle.is_dir(), f"committed example missing: {bundle}")
                with tempfile.TemporaryDirectory() as temp_dir:
                    subprocess.run(
                        [sys.executable, str(ANALYZER), "--bundle", str(bundle), "--output-dir", temp_dir],
                        check=True, capture_output=True, text=True,
                    )
                    report = next(Path(temp_dir).glob("report-*"))
                    analysis = json.loads((report / "analysis.json").read_text(encoding="utf-8"))
                self.assertIn(analysis["validation"]["status"], {"valid", "valid_with_limitations"})
                self.assertEqual({entry["id"] for entry in analysis["observations"]}, observation_ids)

    def test_committed_examples_carry_the_current_versions(self) -> None:
        """A released version with examples from an older one is a stale repository.

        The failure this prevents is not a bug in the tools. It is a reader
        running the current code and comparing it against output an earlier
        version produced, with nothing in the repository saying so.
        """
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from analyze_evidence_bundle import ANALYZER_VERSION
        finally:
            sys.path.pop(0)
        collector_line = next(
            line for line in (PROJECT_ROOT / "collect_systemd_evidence.sh")
            .read_text(encoding="utf-8").splitlines()
            if line.startswith("readonly COLLECTOR_VERSION=")
        )
        collector_version = collector_line.split('"')[1]
        self.assertEqual(collector_version, ANALYZER_VERSION, "the two versions move together")
        for scenario in ("failed_config", "ignored_directive"):
            with self.subTest(example=scenario):
                metadata = json.loads(
                    (PROJECT_ROOT / "examples" / f"evidence_bundle_{scenario}" / "collection.json")
                    .read_text(encoding="utf-8")
                )
                self.assertEqual(metadata["collector_version"], collector_version)
                report = json.loads(
                    (PROJECT_ROOT / "examples" / f"analysis_report_{scenario}" / "analysis.json")
                    .read_text(encoding="utf-8")
                )
                self.assertEqual(report["analysis_version"], ANALYZER_VERSION)

    def test_untrusted_unit_text_never_resolves_to_restart_no(self) -> None:
        """Each way of losing trust in unit-effective.txt, tested on its own."""
        def with_status(status: str):
            def mutate(bundle: Path) -> None:
                rows = []
                for line in (bundle / "command-outcomes.tsv").read_text(encoding="utf-8").splitlines():
                    if line.startswith("unit-effective.txt\t"):
                        fields = line.split("\t")
                        fields[2] = status
                        line = "\t".join(fields)
                    rows.append(line)
                manifest = bundle / "manifest.tsv"
                kept = [
                    entry for entry in manifest.read_text(encoding="utf-8").splitlines()
                    if not entry.startswith("unit-effective.txt\t")
                ]
                manifest.write_text("\n".join(kept) + "\n", encoding="utf-8")
                (bundle / "unit-effective.txt").unlink()
                self.rewrite_outcomes(bundle, rows)
            return mutate

        def unmanifested(bundle: Path) -> None:
            manifest = bundle / "manifest.tsv"
            kept = [
                entry for entry in manifest.read_text(encoding="utf-8").splitlines()
                if not entry.startswith("unit-effective.txt\t")
            ]
            manifest.write_text("\n".join(kept) + "\n", encoding="utf-8")
            self.rewrite_anchor(bundle)

        def digest_mismatch(bundle: Path) -> None:
            path = bundle / "unit-effective.txt"
            path.write_text(path.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")

        cases = {
            "skipped": with_status("skipped"),
            "unavailable": with_status("unavailable"),
            "integrity_error": with_status("integrity_error"),
            "unmanifested": unmanifested,
            "digest mismatch": digest_mismatch,
            "deleted": self.remove_unit_effective,
        }
        for description, mutate in cases.items():
            with self.subTest(case=description):
                analysis = self.analyze_mutated("ignored_directive", mutate)
                self.assertNotIn(
                    "OBS-RESTART-DECLARED-DIFFERS",
                    {entry["id"] for entry in analysis["observations"]},
                    description,
                )

    def test_trusted_unit_text_still_produces_the_comparison(self) -> None:
        """The guard must not silence the case the observation exists for."""
        observation_ids = self.observation_ids("ignored_directive")
        self.assertIn("OBS-RESTART-DECLARED-DIFFERS", observation_ids)

    def test_manifest_listing_itself_or_its_anchor_is_rejected(self) -> None:
        for excluded in ("manifest.tsv", "manifest.sha256"):
            with self.subTest(file=excluded):
                def mutate(bundle: Path, excluded=excluded) -> None:
                    manifest = bundle / "manifest.tsv"
                    row = "\t".join([excluded, "0" * 64, "1", "20260101T000000Z", "collection_record", ""])
                    manifest.write_text(
                        manifest.read_text(encoding="utf-8") + row + "\n", encoding="utf-8"
                    )
                    self.rewrite_anchor(bundle)

                self.assert_invalid_with(self.analyze_mutated("healthy", mutate), "the format excludes")

    def test_trust_state_is_machine_readable(self) -> None:
        analysis = self.analyze_fixture("healthy")
        source = analysis["source_bundle"]
        for field in ("manifest_valid", "outcomes_valid", "metadata_trusted"):
            self.assertIn(field, source)
            self.assertTrue(source[field])

    def test_untrusted_metadata_is_declared_in_both_output_formats(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle = Path(temp_dir) / "mutated"
            shutil.copytree(FIXTURES / "healthy", bundle)
            metadata = json.loads((bundle / "collection.json").read_text(encoding="utf-8"))
            metadata["unit"] = "entirely-different.service"
            (bundle / "collection.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
            report_parent = Path(temp_dir) / "reports"
            subprocess.run(
                [sys.executable, str(ANALYZER), "--bundle", str(bundle), "--output-dir", str(report_parent)],
                check=True, capture_output=True, text=True,
            )
            report = next(report_parent.glob("report-*"))
            markdown = (report / "analysis.md").read_text(encoding="utf-8")
            analysis = json.loads((report / "analysis.json").read_text(encoding="utf-8"))
        self.assertIn("(unverified)", markdown)
        self.assertIn("Journal access", markdown)
        self.assertFalse(analysis["source_bundle"]["metadata_trusted"])

    def rehash_collection_metadata(self, bundle: Path, metadata: dict) -> None:
        """Write metadata and keep its manifest row and the anchor consistent.

        This produces a bundle whose bytes verify and whose contents are wrong,
        which is the case that separates integrity from validity.
        """
        import hashlib

        path = bundle / "collection.json"
        text = json.dumps(metadata, indent=2) + "\n"
        path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        manifest = bundle / "manifest.tsv"
        rows = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.startswith("collection.json\t"):
                fields = line.split("\t")
                fields[1] = digest
                fields[2] = str(len(text.encode("utf-8")))
                line = "\t".join(fields)
            rows.append(line)
        manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.rewrite_anchor(bundle)

    def test_verified_bytes_do_not_make_invalid_metadata_trusted(self) -> None:
        cases = {
            "malformed context": {"collection_context": "malformed"},
            "unsupported schema": {"schema_version": "9.9"},
            "empty unit": {"unit": ""},
            "malformed inputs": {"inputs": "malformed"},
        }
        for description, change in cases.items():
            with self.subTest(case=description):
                def mutate(bundle: Path, change=change) -> None:
                    metadata = json.loads((bundle / "collection.json").read_text(encoding="utf-8"))
                    metadata.update(change)
                    self.rehash_collection_metadata(bundle, metadata)

                analysis = self.analyze_mutated("healthy", mutate)
                self.assertFalse(analysis["source_bundle"]["metadata_trusted"], description)

    def test_manifest_tampering_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            copied_bundle = Path(temp_dir) / "tampered"
            shutil.copytree(FIXTURES / "healthy", copied_bundle)
            target = copied_bundle / "systemctl-show.txt"
            target.write_text(target.read_text(encoding="utf-8") + "\nResult=changed\n", encoding="utf-8")
            report_parent = Path(temp_dir) / "reports"
            completed = subprocess.run(
                [sys.executable, str(ANALYZER), "--bundle", str(copied_bundle), "--output-dir", str(report_parent)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            report = next(report_parent.glob("report-*"))
            analysis = json.loads((report / "analysis.json").read_text(encoding="utf-8"))
            self.assertEqual(analysis["validation"]["status"], "invalid")
            self.assertTrue(any("checksum mismatch" in entry.lower() for entry in analysis["validation"]["errors"]))
            self.assertIn("OBS-BUNDLE-LIMITED", {entry["id"] for entry in analysis["observations"]})

    # --- Outcome status and exit status must agree ---------------------------

    def rewrite_outcome_row(self, bundle: Path, artifact: str, status: str, exit_code: str) -> None:
        """Set one outcome row's status and exit, keeping the manifest consistent.

        The manifest row is updated too, so the only defect in the resulting
        bundle is the status and exit pairing itself rather than a disagreement
        between two records.
        """
        rows = []
        for line in (bundle / "command-outcomes.tsv").read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{artifact}\t"):
                fields = line.split("\t")
                fields[2] = status
                fields[3] = exit_code
                line = "\t".join(fields)
            rows.append(line)
        manifest = bundle / "manifest.tsv"
        updated = []
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{artifact}\t"):
                fields = line.split("\t")
                fields[5] = exit_code
                line = "\t".join(fields)
            updated.append(line)
        manifest.write_text("\n".join(updated) + "\n", encoding="utf-8")
        self.rewrite_outcomes(bundle, rows)

    def test_captured_row_without_an_exit_status_is_rejected(self) -> None:
        """A captured row has to say how the command ended.

        Without this rule a bundle can claim a command ran and produced an
        artifact while recording nothing about how it exited, and the artifact
        still becomes readable evidence.
        """
        def mutate(bundle: Path) -> None:
            self.rewrite_outcome_row(bundle, "journal.txt", "captured", "")

        analysis = self.analyze_mutated("failed_config", mutate)
        self.assert_invalid_with(analysis, "which that status does not permit")
        self.assertFalse(analysis["validation"]["outcomes_valid"])
        self.assertEqual(self.semantic_observation_ids(analysis), set())

    def test_a_status_recording_no_command_run_carries_no_exit_status(self) -> None:
        for artifact, status in (("path-metadata.txt", "skipped"), ("journal.txt", "unavailable")):
            with self.subTest(status=status):
                def mutate(bundle: Path, artifact=artifact, status=status) -> None:
                    self.rewrite_outcome_row(bundle, artifact, status, "0")

                analysis = self.analyze_mutated("failed_config", mutate)
                self.assert_invalid_with(analysis, "which that status does not permit")
                self.assertFalse(analysis["validation"]["outcomes_valid"])

    def test_permitted_status_and_exit_pairings_stay_valid(self) -> None:
        """The rule must not reject what the collector legitimately writes."""
        sys.path.insert(0, str(PROJECT_ROOT))
        try:
            from analyze_evidence_bundle import valid_outcome_exit
        finally:
            sys.path.pop(0)
        for status, exit_code in (
            ("captured", "0"),
            ("captured", "22"),
            ("captured", "255"),
            ("skipped", ""),
            ("unavailable", ""),
            ("integrity_error", ""),
            ("integrity_error", "22"),
        ):
            with self.subTest(status=status, exit_code=exit_code):
                self.assertTrue(valid_outcome_exit(status, exit_code))
        for status, exit_code in (
            ("captured", ""),
            ("captured", "256"),
            ("captured", "abc"),
            ("skipped", "0"),
            ("unavailable", "1"),
        ):
            with self.subTest(status=status, exit_code=exit_code):
                self.assertFalse(valid_outcome_exit(status, exit_code))

    # --- Report output must not land inside the bundle it describes ----------

    def run_analyzer(self, bundle: Path, output_directory: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ANALYZER), "--bundle", str(bundle), "--output-dir", str(output_directory)],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_output_directory_inside_the_bundle_is_refused(self) -> None:
        """Reports written into the bundle would break its own manifest."""
        with tempfile.TemporaryDirectory() as temp_dir:
            copied_bundle = Path(temp_dir) / "bundle"
            shutil.copytree(FIXTURES / "healthy", copied_bundle)
            for description, output_directory in (
                ("the bundle itself", copied_bundle),
                ("a directory inside the bundle", copied_bundle / "reports"),
                ("a directory deeper inside the bundle", copied_bundle / "reports" / "today"),
            ):
                with self.subTest(output=description):
                    completed = self.run_analyzer(copied_bundle, output_directory)
                    self.assertEqual(completed.returncode, 2, completed.stderr)
                    self.assertIn("directory inside it", completed.stderr)

    def test_output_directory_beside_or_above_the_bundle_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            copied_bundle = Path(temp_dir) / "parent" / "bundle"
            copied_bundle.parent.mkdir(parents=True)
            shutil.copytree(FIXTURES / "healthy", copied_bundle)
            for description, output_directory in (
                ("a sibling of the bundle", copied_bundle.parent / "reports"),
                ("the bundle's own parent", copied_bundle.parent),
                ("an unrelated directory", Path(temp_dir) / "elsewhere"),
            ):
                with self.subTest(output=description):
                    completed = self.run_analyzer(copied_bundle, output_directory)
                    self.assertEqual(completed.returncode, 0, completed.stderr)

    # --- Metadata validity is a type and vocabulary contract -----------------

    def test_metadata_field_types_and_vocabularies_are_enforced(self) -> None:
        """Each case verifies bytes correctly and still says something unusable."""
        cases = {
            "mode outside the vocabulary": {"mode": "live_read_write"},
            "mode of the wrong type": {"mode": 1},
            "collection_status outside the vocabulary": {"collection_status": "mostly_fine"},
            "artifact_count as a string": {"artifact_count": "10"},
            "artifact_count as a boolean": {"artifact_count": True},
            "artifact_count negative": {"artifact_count": -1},
            "dry_run as a string": {"dry_run": "false"},
            "journal_until_utc absent": {"journal_until_utc": ""},
            "inputs flag as a string": {"inputs": {"health_url_supplied": "yes"}},
            "health_target as a number": {"inputs": {"health_target": 8088}},
            "euid as a string": {"collection_context": {"euid": "0"}},
            "journal_access outside the vocabulary": {"collection_context": {"journal_access": "full"}},
            "systemd_version as a number": {"collection_context": {"systemd_version": 259}},
            "fixture_evidence flag as a string": {"fixture_evidence": {"health_probe_simulated": "true"}},
        }
        for description, change in cases.items():
            with self.subTest(case=description):
                def mutate(bundle: Path, change=change) -> None:
                    metadata = json.loads((bundle / "collection.json").read_text(encoding="utf-8"))
                    for key, value in change.items():
                        if isinstance(value, dict) and isinstance(metadata.get(key), dict):
                            metadata[key].update(value)
                        else:
                            metadata[key] = value
                    self.rehash_collection_metadata(bundle, metadata)

                analysis = self.analyze_mutated("healthy", mutate)
                self.assertEqual(analysis["validation"]["status"], "invalid", description)
                self.assertFalse(analysis["source_bundle"]["metadata_trusted"], description)

    def test_metadata_tolerates_a_field_the_schema_does_not_name(self) -> None:
        """A newer collector adding a field must not make the bundle invalid."""
        def mutate(bundle: Path) -> None:
            metadata = json.loads((bundle / "collection.json").read_text(encoding="utf-8"))
            metadata["future_field"] = {"added_by": "a later collector"}
            self.rehash_collection_metadata(bundle, metadata)

        analysis = self.analyze_mutated("healthy", mutate)
        self.assertEqual(analysis["validation"]["status"], "valid")
        self.assertTrue(analysis["source_bundle"]["metadata_trusted"])

    # --- Every observation must point at a section that exists ---------------

    def test_every_observation_reference_resolves_in_the_review_guide(self) -> None:
        """A renamed observation must not leave a link pointing at nothing."""
        def heading_anchor(heading_text: str) -> str:
            slug = heading_text.strip().lower().replace(" ", "-")
            return "".join(character for character in slug if character.isalnum() or character == "-")

        guide = (PROJECT_ROOT / "docs" / "review_guide.md").read_text(encoding="utf-8")
        anchors = {
            heading_anchor(line[3:])
            for line in guide.splitlines()
            if line.startswith("## ")
        }
        self.assertIn("limited-bundle", anchors)
        seen = set()
        for fixture in ("healthy", "failed_config", "permission_failure",
                        "dependency_failure", "incomplete_bundle", "ignored_directive"):
            for observation in self.analyze_fixture(fixture)["observations"]:
                reference = observation["playbook_reference"]
                self.assertIn("#", reference, observation["id"])
                path, _, anchor = reference.partition("#")
                self.assertEqual(path, "docs/review_guide.md", observation["id"])
                self.assertIn(anchor, anchors, f"{observation['id']} points at a missing section")
                seen.add(observation["id"])
        self.assertIn("OBS-BUNDLE-LIMITED", seen)


if __name__ == "__main__":
    unittest.main()
