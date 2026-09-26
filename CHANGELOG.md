# Changelog

Dates are UTC. Versions are the `COLLECTOR_VERSION` and `ANALYZER_VERSION`
constants in the two tools; they move together.

## 0.11.0 (2026-09-26)

The collector and the analyzer are byte-identical to 0.10.0.

### Removed

- **`test/check_shell_style.sh` and its wiring.** It enforced the author's
  shell-authoring convention, that every variable expansion uses `${brace}`
  notation. That is a property of how this repository is written rather than a
  property of what these tools do, and a reader cloning an evidence collector
  has no reason to run it or to have their build fail on it. It now lives in the
  author's own toolchain, where it covers every repository instead of one.
  `test/run_all_tests.sh` no longer calls it, and the README no longer lists it.

  It had also earned the removal. The gate searched with `grep -P` and piped the
  result into a `while` loop. BSD grep, macOS grep, and busybox grep all reject
  `-P`, and the pipe turned that refusal into an empty result, so on any of those
  hosts it printed `brace-notation style check passed` and exited 0 against a
  file carrying violations. Reproduced with a shimmed grep on a file holding two
  bare expansions: two `invalid option` messages on stderr, a pass on stdout,
  exit 0. A checker reporting a result it had not earned, in the test directory
  of a repository about checkers that report results they have not earned.

### Changed

- **The placeholder service name is now `example-api.service` everywhere.** The
  synthetic fixtures and the README's usage examples had carried two different
  invented service names, and neither one announced itself as invented. One name
  now covers the role, under `/opt/example-api`, because RFC 2606 reserves
  `example` for documentation and a reader should never have to work out whether
  a fixture describes something real. Both committed example bundles and their
  reports were regenerated.

## 0.10.0 (2026-09-26)

An eighth review pass found no new defect in what the tools do. Every item here
is a place where a stated contract was looser than the sentence describing it,
which in an evidence tool is the same class of problem.

### Fixed

- **A `captured` outcome row did not have to say how the command exited.**
  `valid_process_exit` accepted an empty string for every status, so a row could
  claim a command ran and produced an artifact while recording nothing about its
  exit, and that artifact still became readable evidence. Exit status is now
  validated against the status that carries it: `captured` requires a value from
  0 to 255, `skipped` and `unavailable` require none, and `integrity_error`
  accepts either, because that failure can happen before the command runs or
  after it has exited. Three tests cover it, including one that asserts the
  permitted pairings the collector actually writes are still accepted.
- **The `--output-dir` error said the opposite of what the code prevents.** The
  guard rejects an output directory inside the bundle, which is correct, because
  reports written there would be files `manifest.tsv` does not cover. The
  message said "or a parent of it." It now says "or a directory inside it," and
  six cases test both sides of the boundary.
- **`metadata_valid` claimed more than it checked.** Validation covered five
  required strings and the shape of four nested objects, so a `collection.json`
  with `artifact_count` as a string, a `mode` the collector never emits, or a
  `journal_access` value outside the vocabulary passed as valid metadata.
  Every field the analyzer reads is now checked against its type and, where it
  has one, its vocabulary. Fourteen cases are tested, each rehashed so the
  bundle's bytes still verify, which is the case that separates an authentic
  record from a usable one. A field the schema does not name is left alone, so a
  later collector can add one without invalidating the bundle.

### Changed

- **`OBS-BUNDLE-INCOMPLETE` is now `OBS-BUNDLE-LIMITED`.** The observation fires
  on validation errors, on evidence that was not captured, and on limitations
  alone. A bundle with complete verified evidence and one nonzero health probe
  is limited, not incomplete, and the identifier said otherwise. The review
  guide section is now "Limited bundle" and separates the three cases, since the
  right next step differs for each.
- A test now walks every observation the six fixtures can produce and checks its
  reference against the headings in `docs/review_guide.md`. The rename above
  would have broken every link silently; nothing in the suite would have caught
  it.
- README documents the health target as a known limit. The probe URL is written
  to `collection.json` verbatim and redaction does not touch it, so the
  hostname, port, and path travel with the bundle.

### Rejected

- **A reported Python 3.9 incompatibility in the analyzer's `X | None`
  annotations.** `from __future__ import annotations` is the first statement in
  both Python files, so annotations are never evaluated at runtime and the
  syntax is valid on 3.9. Verified with `ast.parse(feature_version=(3, 9))`; the
  only runtime `|` in each file is a set union. The CI matrix runs 3.9.
- **A proposal that invalid metadata should withdraw trust from every artifact.**
  Metadata integrity and artifact integrity are separate dimensions. Collapsing
  them would let one malformed field erase an otherwise sound evidence bundle,
  which is the opposite of what this tool is for.

## 0.9.0 (2026-09-26)

A seventh review pass found a defect that punched through the safety claim
itself rather than the reporting around it.

### Fixed

- **The health probe read `~/.curlrc`.** curl treats that file's lines as
  command-line options unless `-q` comes first, so a file in the caller's home
  directory could change the request method, add a second URL, or redirect the
  output to a path outside the bundle. Reproduced before fixing: a `.curlrc`
  carrying `output = /tmp/escaped.txt` wrote 320 bytes there and left the
  collector's own artifact empty. The probe now runs `curl -q`, and a test
  starts a local listener, plants that `.curlrc`, and fails if the outside file
  appears or the flag disappears.
- **Metadata trust conflated integrity with validity.** `metadata_trusted` meant
  only that the manifest row verified, so a correctly hashed `collection.json`
  full of malformed metadata was still presented as fact. Trust now requires a
  verified row and metadata that passes validation, with four cases tested.

### Changed

- README documents the curl configuration boundary, the two-part metadata trust
  rule, and the semantics of a missing `manifest.sha256`.

## 0.8.0 (2026-09-24)

A sixth review pass caught a defect worse than the one it replaced: the trust
check for unit text was written, and then the code went on to ignore it.

### Fixed

- **`unit_text_trusted` was computed and never used.** The restart comparison
  still ran on untrusted text, so a missing `unit-effective.txt` could be
  reported as a unit file carrying no `Restart=` assignment. The entire
  comparison now lives inside the trust branch, and six separate cases are
  tested: skipped, unavailable, integrity error, unmanifested, digest mismatch,
  and deleted. A seventh asserts the observation still fires when the text is
  trusted, so the guard cannot silence the case it exists for.
- **`journal_access` bypassed the unverified label.** Every value drawn from
  `collection.json` now carries the same label when that record failed.
- **The trust state was not machine-readable.** `analysis.json` now exposes
  `manifest_valid`, `outcomes_valid`, and `metadata_trusted` under
  `source_bundle`, so the Markdown and JSON reports state the same policy.
- **The manifest could list itself or its anchor.** Both are rejected, with a
  test for each.
- **The CLI safety text claimed the collector never copies or deletes.** It does
  both, to its own files. The help now names the protected object: no systemd or
  service state changes, while its own files are written, moved, and removed.
- **`artifact_count` relied on write order.** The exclusions are now named in the
  command, matching the analyzer's definition rather than depending on
  `collection.json` not existing yet.
- The report labels the manifest digest as computed, since it is recalculated
  at analysis time rather than read from the anchor.

## 0.7.0 (2026-09-24)

Changes from a fifth adversarial review pass. Three reviewers converged on the
same defect: the analyzer treated the outcome record as part of the trust
decision and as ordinary input that stayed usable after being declared invalid.

### Fixed

- **A malformed outcome record could still authorize reading content.** Outcome
  errors set no trust state, so a duplicated or unrecognized row left the bundle
  invalid while the manifest stayed valid and artifact content still drove
  observations. Outcome validity is now its own dimension, and either half
  failing withdraws trust from all content.
- **Three different lookups resolved a duplicated outcome row three ways.** One
  validated map is built once and used everywhere.
- **An integrity error could be reported as a failing health probe.** A health
  row with a nonzero exit and an artifact that was never retained triggered
  `OBS-ACTIVE-HEALTH-FAIL`. Semantic rules now read exit status only through a
  trusted accessor: captured, manifest-verified, or nothing.
- **Missing unit text became an asserted `Restart=no`.** An absent
  `unit-effective.txt` and a captured one with no `Restart=` were the same value,
  so the report could describe a unit file it never read. Availability and
  meaning are now separate, and the comparison runs only on trusted text.
- **Malformed nested metadata could raise.** A `collection_context` that is a
  string, not an object, produced an `AttributeError` outside the handled
  exceptions. Nested objects are type-checked during validation.
- **Unverified metadata framed the report.** An altered `collection.json` still
  supplied the unit name, timestamp, and mode in the Scope table. Those fields
  are now labelled `(unverified)` when that record's manifest row did not verify.
- **A health temporary-file failure aborted the rest of live collection.** The
  later steps the operator asked for never ran and never got outcome rows. The
  failure is recorded and collection continues.
- **Manifest finalization skipped missing required records and vanished captured
  artifacts.** Both now fail the collection rather than publishing a bundle the
  analyzer will reject.
- **The bundle directory's mode was set without checking**, in a tool whose
  output-privacy claim depends on it.
- **Exit statuses were validated inconsistently.** Outcomes accepted negatives
  and the manifest accepted 999. One validator now enforces 0 to 255.
- Carriage returns are stripped from outcome notes.
- `cat` is preflighted, since the metadata writers use it.

### Added

- Tests for each trust case above, including outcome mutations kept internally
  consistent with the manifest and anchor, so they prove trust withdrawal rather
  than mere detection.
- A test that analyzes both committed examples and asserts their observation
  sets, so they cannot drift from the code while the suite passes.
- `inputs.health_target` records the URL the operator supplied, which is what
  the review guide tells a reviewer to check.
- `fixture_evidence` declares scenario-generated health and path artifacts, so
  fixture metadata never implies operator input.

### Changed

- README: dependency table split by mode and matched to the preflight list;
  "usage error" corrected to environment error; the copy/delete claim scoped to
  systemd and service state; "deterministic synthetic scenarios" rather than
  bundles; `artifact_count` defined where it is listed.
- The review guide points at `inputs.health_target`, and its active-but-unhealthy
  section no longer says what a failing probe usually means.
- Test harness capture files live in the per-run temporary directory.

## 0.6.0 (2026-09-24)

Changes from a fourth adversarial review pass, which re-read the implementation
rather than the prose and found one defect I had introduced myself.

### Fixed

- **`systemctl --version_text` was not a real option.** A mechanical rename in
  0.5.0 rewrote the flag along with the variable, so every live bundle recorded
  `systemd_version: unknown`. A test now asserts that a live bundle records a
  version string beginning with `systemd`, and another fails on any option
  carrying a variable-naming suffix. Four prose corruptions from the same rename
  are corrected.
- **The analyzer read artifact content the bundle never vouched for.** An
  observation rule read a file if it existed, so a file added after collection,
  or one the outcome record called skipped, could drive an observation while the
  citation filter quietly swapped the evidence reference. Reads now go through a
  trust gate: captured in `command-outcomes.tsv` and covered by a manifest row
  that verified. When the manifest is in question, no content is read at all.
- **`manifest_valid` could stay true after the manifest failed to parse or was
  missing.** It is now false on every parse, availability, or structural failure.
- **Limitation-producing reads happened after validation was finalized**, so a
  read error could try to append to an immutable tuple. Every read now runs
  before the result is built.
- **Collection records were written without checking.** `command-outcomes.tsv`,
  `collection.json`, `collector-summary.md`, `manifest.tsv`, and
  `manifest.sha256` now publish through a checked writer that renames on success,
  and a failure ends the collection with an environment error rather than
  printing a success message over an incomplete bundle.
- **Fixture writes bypassed the checked path** for `health.stderr` and
  `unit-verify.txt`. They no longer do.
- **The summary claimed health was captured** even when its redaction failed. It
  now reports the recorded status.
- **Manifest values were not validated at write time.** Each digest must match
  `[0-9a-f]{64}` and each byte count must be numeric before the row is written,
  and the analyzer enforces the same shapes plus a plausible exit status.

### Changed

- The style gate builds its pattern in a named variable with an explicit
  ShellCheck exemption, so the suite is clean at `--severity=style`.
- README states the trust rule for artifact content, the journal window's two
  different bound types, and a softer comparison with `sos report`.

## 0.5.0 (2026-09-24)

Changes from a third adversarial review. Every item was a place where the
implementation recorded success from control flow rather than from a verified
result, or where a claim outran the code.

### Fixed

- **The health probe ignored redaction failures.** `capture_command` checked
  whether redaction succeeded; the health path did not, so a failed transform
  could still be recorded as `captured` after the raw source was deleted. Both
  health artifacts now fail together into `integrity_error` and are not retained.
- **Redaction published in place.** `redact_to_file` wrote directly to the
  destination, so a failed `sed` could leave a truncated artifact behind. It now
  writes to a temporary file and renames on success only.
- **Static writes were never checked.** `capture_static_text` recorded `captured`
  without confirming the write or the mode. It now records `integrity_error` and
  removes the file when either fails.
- **The journal window claimed UTC without saying so.** `--until` received a bare
  timestamp, which systemd reads in the host's local zone, while
  `journal_until_utc` recorded it as UTC. The boundary now carries an explicit
  `UTC` suffix.
- **An existing output directory had its mode rewritten.** Both tools chmod'd the
  parent they were given, so `--output-dir /tmp` as root would have set `/tmp` to
  0700. An existing parent is now left alone; only a parent the tool creates, and
  the bundle itself, are made private.
- **`hostname_included: false` was untrue.** Journal output carries hostnames.
  The field is now `hostname_redaction_applied` and `hostname_may_be_present`.
- **A header-only manifest passed validation.** An empty manifest skipped every
  coverage check and still validated. It is now invalid.
- **Validation status could be decided before every finding existed.** Fact
  parsing appended limitations to an already-finalized result, so a report could
  read `valid` and then list a problem. Findings are collected in a mutable
  draft, and the immutable result is built once, after every producer has run.
- **A malformed TSV row could crash the analyzer.** Parsing now enforces the
  exact header and field count, and rejects unknown outcome statuses, non-numeric
  exit codes, unsafe artifact names, and duplicate outcome rows.
- **A symlinked bundle path was accepted.** The symlink check ran after
  `resolve()`, which had already followed it. It now runs first.
- **Report directories could collide.** Two analyses of the same unit in the same
  second now get distinct directories.
- **`Restart=` reset was recognized and then skipped.** An absent assignment and
  an explicit reset both mean `Restart=no`, and both are now compared against
  what the manager reports.
- **The loopback health probe could follow a proxy.** `curl` now runs with
  `--noproxy '*'`.
- **Missing commands were reported as usage errors.** They are an environment
  problem and now exit 3 with their own message. The required list also covers
  `mv`, `rm`, `chmod`, `head`, `tail`, and `bash`.
- **Declared schema support outran the validator.** Schema 1.0 and 1.1 bundles
  would be rejected by the new coverage rules, so only 1.2 is declared.

### Added

- `test/check_shell_style.sh`, run first by the suite: every shell variable
  expansion must use `${brace}` notation.
- Tests for each fix above, including a stubbed `sed` that forces redaction to
  fail, a mode check on a pre-existing output directory, and a same-second
  double analysis.
- Markdown escaping for every bundle-supplied value in a report.

### Changed

- All shell variables use `${brace}` notation and full words: `BUNDLE_DIR` is
  `EVIDENCE_BUNDLE_DIRECTORY`, `OUTCOMES` is `COMMAND_OUTCOMES_FILE`, and so on.
- The restart predicate is split into named conditions, because an evidence tool
  whose logic needs a precedence check before it can be read is not auditable.
- Facts are parsed only from a `systemctl-show.txt` recorded as captured, and the
  report says so when the command behind it exited nonzero.

## 0.4.0 (2026-09-24)

Changes from a second adversarial review, run against the 0.3.0 tree by three
independent reviewers. Every item below was a claim the project made that the
implementation did not fully support.

### Fixed

- **Redaction could leave part of a secret behind.** The old value pattern
  stopped at whitespace, so `Environment="TOKEN=abc 123"` became
  `Environment="TOKEN=[REDACTED] 123"`. The filter now blanks to end of line on
  a name match, covers more names case-insensitively, and blanks `user:password`
  pairs inside URLs. Tests assert that no known secret value survives.
- **The manifest did not cover the collection records.** `command-outcomes.tsv`,
  `collection.json`, and `collector-summary.md` were unhashed, so the layer that
  says how to read the evidence could be changed without a mismatch. They are in
  the manifest now.
- **The analyzer verified only what the manifest listed.** It now rejects a
  manifest that omits a captured artifact, duplicates a row, omits a collection
  record, disagrees with `command-outcomes.tsv` on command class or exit code,
  or leaves a file in the bundle unlisted.
- **Fixture metadata described the scenario as operator input.** A fixture run
  without `--health-url` reported `health_url_supplied: true`. Operator flags now
  live in an `inputs` block and reflect what was actually passed.
- **Reports cited evidence files that were never captured.** Observation evidence
  lists are filtered against `command-outcomes.tsv`, and a test walks every
  fixture to enforce it.
- **Two runs in the same second collided.** Bundle directory names carry a
  process-id suffix.
- **An absent `Restart=` and an explicit `Restart=` reset were both `None`.**
  They are now `ABSENT` and `RESET`.
- **A missing command became evidence.** Required commands are checked up front
  and a missing one is a usage error. `usage()` no longer needs an external
  command to print.
- `json_escape` now escapes tabs and carriage returns.

### Added

- `docs/review_guide.md`. Reports previously referenced `PB-LNX-001` and
  `RB-LNX-001`, which do not ship with this repository, so the tool did not stand
  on its own. Every observation now points at a section of this file.
- The journal query is bounded at both ends, and `journal_until_utc` is recorded.
- The host systemd version is recorded in `collection.json`.
- A validation matrix in the README, including the rows that are unrecorded.
- Adversarial bundle tests: omitted artifact, duplicate row, unlisted file,
  modified metadata, modified outcomes, exit-code disagreement, altered anchor.

### Changed

- Collection schema is 1.2. `collection_status` now takes `complete`,
  `complete_with_command_failures`, or `partial`, each defined in the README.
- "Read-only" is stated as service-state read-only, and "no dependencies" as no
  third-party Python packages, with the system commands listed.
- The report's opening line describes itself as evidence-linked and
  observational with bounded review guidance, which is what it actually is.
- Journal-visibility wording allows for ACLs, distribution differences, and
  journal retention.

## 0.3.0 (2026-09-24)

Changes prompted by an adversarial review of the 0.2.0 tree, in the order a
hostile reader would hit them.

### Fixed

- A live run labelled `process.txt` with the note `synthetic fixture artifact`
  when no MainPID was present. An evidence file that misreports its own origin
  is a defect, not a cosmetic one. Static-text capture now takes the note from
  its caller, and a test fails if any live bundle carries fixture wording.
- `collection_status` was the literal string `complete_with_observations` on
  every run, including runs where commands failed. It is now derived from
  `command-outcomes.tsv`: `complete`, `complete_with_N_nonzero_command_exits`,
  or `partial_N_artifact_not_captured`.

### Added

- `manifest.sha256`, holding the digest of `manifest.tsv`, also printed on
  stdout so it can be recorded off the host. The analyzer verifies it.
- Privilege context in `collection.json`: effective UID and a `journal_access`
  value. A caller outside root, `systemd-journal`, and `adm` sees a silently
  filtered journal, and the analyzer now states that in the report.
- `test/live_smoke.sh`, a read-only check against a real systemd host. It
  refuses to run unless systemd is PID 1.
- Tests that assert the safety boundary the README claims: no `sudo`, no
  state-changing `systemctl` verb, no writes outside the output directory.
- README sections on prior art, the limits of the manifest, the limits of
  redaction, exit codes, and license terms.

### Changed

- Collection schema is 1.1. The `privacy` block names its filter
  (`redaction_filter`, `redaction_scope`) instead of asserting
  `redaction_applied: true`, which read as a guarantee the four patterns do not
  provide. The analyzer accepts 1.0 and 1.1, and records a limitation for 1.0.
- The report header carries the collection mode, the journal access value, and
  the manifest digest.

## 0.2.0 (2026-09-22)

### Fixed

- The live `systemctl show` property list omitted `Restart`, so a bundle could
  not show which restart policy the manager actually held.
- The fixture bundles reported unit verification as completing "without a static
  unit error", which is not what a clean verifier run looks like.

### Added

- `--include-unit-verify` records the verifier's exit code and its diagnostic
  line count in one row, because `systemd-analyze verify` reports an ignored
  directive on stderr and still exits 0.
- Fixture scenario `ignored_directive`: a unit whose `Restart=on-failuer` was
  discarded at load, killed by a signal, reporting `Restart=no`.
- Analyzer observations `OBS-UNIT-VERIFY-DIAGNOSTICS` and
  `OBS-RESTART-DECLARED-DIFFERS`.
- Committed examples for `failed_config` and `ignored_directive`.

### Changed

- Every script is invoked through `bash` in documentation and tests, so a clone
  that dropped the executable bit still runs.

## 0.1.0 (2026-09-21)

First working tree: read-only collector, analyzer, five fixture scenarios,
manifest with digests, and the observation vocabulary.
