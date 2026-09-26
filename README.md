# systemd-evidence-collector

Two tools for a failed systemd service. The collector captures a private
evidence bundle. The analyzer reads that bundle and writes a human review
report. Neither changes systemd or service state, and neither names a cause.

## The job this removes

A service has failed and someone needs to look at it. The usual sequence is a
handful of `systemctl` and `journalctl` invocations typed from memory, read on
screen, and then lost. The next person repeats them against a system that has
moved on, and by the time the question reaches a review nobody can say what the
unit actually reported at the time.

The collector writes that evidence down once, with a manifest and a record of
which commands succeeded. The analyzer turns it into a report that separates
what was captured from what a person still has to decide.

## What it looks like

A unit whose `Restart=on-failuer` was discarded at load, collected and analyzed:

```bash
bash collect_systemd_evidence.sh --unit example-api.service \
  --output-dir ./evidence --include-unit-verify;
python3 analyze_evidence_bundle.py --bundle ./evidence/evidence-<stamp> \
  --output-dir ./reports;
```

The bundle records what each command did:

```text
artifact              command_class    status     exit_code  note
systemctl-show.txt    systemctl_show   captured   0          command exit preserved as evidence
journal.txt           journalctl_unit  captured   0          command exit preserved as evidence
unit-verify.txt       unit_verify      captured   0          exit 0; 1 diagnostic line(s); exit 0 with diagnostics is not a clean result
```

The report states what was seen, and stops:

```text
| **Journal access**  | full_via_adm_group |
| **Validation status** | valid |

OBS-UNIT-FAILED (review)
OBS-UNIT-VERIFY-DIAGNOSTICS (review)
OBS-RESTART-DECLARED-DIFFERS (review)
  The captured unit text assigns Restart=on-failuer, but the manager reports
  Restart=no.
  Evidence: unit-effective.txt, systemctl-show.txt, unit-verify.txt
  Next: docs/review_guide.md#configuration-signals
```

No cause, no ranking, no recommended command. `examples/` holds both of these
in full.

## What already exists, and why this is separate

`sosreport` and `sos report` collect far more than this, and that broader
collection model fits vendor support cases. They run as root, gather system-wide state, and
produce an archive measured in megabytes. This collects one unit, runs as
whoever invokes it, and produces a directory a person can read in a few minutes.

`systemctl status` and `journalctl` are what this runs. The difference is that
their output normally lands on a terminal and is gone. This writes it down with
a record of which commands succeeded, which failed, and what the exit codes were.

`systemd-analyze verify` is a static checker. It reads unit files and never sees
runtime state. This captures both and keeps them side by side.

What this is not: a monitoring agent, a diagnosis engine, a remediation tool, or
a forensic chain-of-custody system. It names no cause and takes no action.

## Safety boundary

The precise claim is **service-state read-only**: the collector performs no
systemd or service state change. It never invokes `sudo`, and it issues no
start, stop, restart, reload, reset, enable, disable, mask, unmask, kill, edit,
copy, or delete against systemd or service state. It does create and remove its
own temporary files, which is a different thing. It reads unit state, a bounded slice of the journal, process
information, and optionally a loopback health URL and path metadata. File
contents at `--path` are never read, only metadata.

It is not read-only in the absolute sense, and saying so would be sloppy. It
creates a directory, writes files, and sets modes, all inside the output
directory you name. With `--health-url` it issues one HTTP GET to a loopback
address you supply, and a GET is not guaranteed side-effect free on the
application's side. That request runs `curl -q`, which stops curl reading
`~/.curlrc`. Without it, a file in the caller's home directory can supply
options as if they were arguments: change the method, add a second URL, or
redirect the output to a path outside the bundle. A test reproduces that
redirection and fails if the flag goes away.

The safety boundary depends on PATH resolving to the real system commands. A
caller who controls PATH controls what the collector runs, so run it in a
trusted environment. The collector fails with an environment error, exit 3, when a required command
is missing, rather than recording the failure as evidence about the unit.

The analyzer runs no system commands and opens no network connections. It reads
a bundle directory and writes a report directory.

Output is created under `umask 077`.

## Requirements

**No third-party Python packages**, and no `requirements.txt`. That is the claim
worth making, because it is why the analyzer runs on a host where installing
anything is a change request.

It is not dependency-free. The analyzer needs Python 3.9 or later and the
standard library. The collector needs Bash 4 and these system commands:

| When | Commands |
|---|---|
| Always, including fixture mode | `awk`, `sed`, `grep`, `tr`, `find`, `date`, `mkdir`, `mktemp`, `mv`, `rm`, `chmod`, `cat`, `head`, `tail`, `sha256sum`, `wc`, `id` |
| Live collection | `systemctl`, `journalctl`, `ps` |
| With `--health-url` | `curl` |
| With `--path` | `bash`, `namei`, `stat` |
| With `--include-unit-verify` | `systemd-analyze` |

That table is the preflight list. The collector checks for exactly these before
it collects anything, and a test fails if the two drift apart.

## Usage

Collect from a live unit:

```bash
bash collect_systemd_evidence.sh --unit example-api.service --output-dir ./evidence;
```

Collect with a health probe and a path to describe, where a failing probe should
still leave a bundle behind:

```bash
bash collect_systemd_evidence.sh \
  --unit example-api.service \
  --output-dir ./evidence \
  --since '-2 hours' \
  --health-url http://127.0.0.1:8088/healthz \
  --path /opt/example-api;
```

Analyze a bundle:

```bash
python3 analyze_evidence_bundle.py --bundle ./evidence/evidence-<stamp>-example-api --output-dir ./reports;
```

Print the plan without collecting:

```bash
bash collect_systemd_evidence.sh --unit example-api.service --output-dir ./evidence --dry-run;
```

Scripts are invoked through `bash` throughout this README, so they run from a
clone that dropped the executable bit.

Include static verification of the unit file:

```bash
bash collect_systemd_evidence.sh --unit example-api.service --output-dir ./evidence --include-unit-verify;
```

## Unit verification and exit status

`--include-unit-verify` runs `systemd-analyze verify` against the unit's fragment
path and keeps everything it prints. That matters because the verifier reports an
ignored directive on stderr and still exits 0. Run against systemd 255, a unit
containing `Restart=on-failuer` produces:

```text
/tmp/lv/t.service:6: Failed to parse service restart specifier, ignoring: on-failuer
```

and exit status 0. A check that reads only the exit status would record that unit
as clean. The collector writes the exit code and the number of diagnostic lines
into the same row of `command-outcomes.tsv`, so neither can be read without the
other.

The analyzer adds two observations from this evidence:

- `OBS-UNIT-VERIFY-DIAGNOSTICS` fires when the verifier printed anything,
  whatever its exit status.
- `OBS-RESTART-DECLARED-DIFFERS` fires when the last `Restart=` assignment in the
  captured `[Service]` text differs from the `Restart` value the manager reports.
  Drop-ins are read in the order `systemctl cat` prints them, and an empty
  assignment counts as a reset.

Neither observation names a cause. A mismatch says the declared restart policy
may not be the one in effect, which is a question for a person to settle.

The article [The Directive That Was Ignored](https://revisualized.com/articles/the-directive-that-was-ignored/)
covers the verifier behavior in detail.

## Synthetic scenarios

The collector generates deterministic synthetic scenarios with `--fixture`, which
is how the test fixtures and the worked examples in this repository are made. No
real host is involved:

```bash
bash collect_systemd_evidence.sh --unit example-api.service --output-dir ./out --fixture failed_config;
```

Available scenarios: `healthy`, `failed_config`, `permission_failure`,
`dependency_failure`, `incomplete_bundle`, `ignored_directive`.

`ignored_directive` is a unit whose `Restart=on-failuer` was discarded at load.
It was killed by a signal, reports `Restart=no` and `NRestarts=0`, and stayed
down. Its verify output uses the diagnostic format systemd 255 printed for the
same defect.

## What a bundle contains

| File | Contents |
|---|---|
| `systemctl-show.txt` | Machine-readable unit properties |
| `systemctl-status.txt` | Status output as an operator would read it |
| `journal.txt` | Journal entries for the requested window |
| `unit-effective.txt` | Effective unit configuration |
| `process.txt` | Process information for the unit |
| `health.txt`, `health.stderr` | Health probe result, when `--health-url` is given |
| `path-metadata.txt` | Ownership and mode along a path, when `--path` is given |
| `unit-verify.txt` | Verifier output, when `--include-unit-verify` is given |
| `command-outcomes.tsv` | Exit status of every command the collector ran |
| `manifest.tsv` | Every evidence artifact and collection record present before manifest finalization, with size and digest. It excludes itself and `manifest.sha256`. |
| `manifest.sha256` | Digest of `manifest.tsv`, also printed on stdout. A missing anchor is recorded as a limitation and does not by itself invalidate the manifest, since anyone able to rewrite the bundle can rewrite the anchor; its value comes from being recorded off the host. |
| `collection.json` | Machine-readable collection record. `artifact_count` counts the evidence artifacts and collection records, excluding `manifest.tsv`, `manifest.sha256`, and itself. `inputs.health_target` records the probe URL as supplied, so treat a path that identifies something sensitive as part of the bundle's contents. |
| `collector-summary.md` | Summary for a person opening the bundle first |

Every observation in a report points at a section of `docs/review_guide.md` in
this repository. Nothing in the output references a document you do not have.

`command-outcomes.tsv` is the file that matters most in a review. A bundle where
a command failed is a different artifact from a bundle where the command
succeeded and found nothing, and the two are indistinguishable without it.

## What the report says

The analyzer emits `analysis.md`, `analysis.json`, and `validation.txt`. The
report states parsed state facts, a validation status for the bundle, and
observations flagged for human review with the evidence file behind each one.

Observations are named for what was seen rather than what it means:
`OBS-UNIT-FAILED` records that the unit reported a failed state. It carries a
pointer to the playbook section a person should work through, and stops there.

The report opens by stating that it is evidence-linked and observational with
bounded review guidance: it names no cause and authorizes no state change. The
guidance it does carry is of the form "read this file next" and "do not widen
permissions as a shortcut," which is review direction rather than remediation.

Severities (`review`, `caution`, `information`) classify attention. They do not
rank causes by likelihood, which is the ranking this tool refuses to invent.

## Repository map

| Path | What it is |
|---|---|
| `collect_systemd_evidence.sh` | The collector. Reads one unit, writes a bundle. |
| `analyze_evidence_bundle.py` | The analyzer. Reads a bundle, writes a report. |
| `docs/review_guide.md` | Where every observation points. Read next, and where to stop. |
| `test/run_all_tests.sh` | The suite. Regenerates fixtures, then runs both test files. |
| `test/generate_fixtures.sh` | Builds the six fixture bundles from the collector's own fixture mode. |
| `test/test_collector.sh` | Collector contract, safety boundary, redaction, metadata. |
| `test/test_analyzer.py` | Analyzer observations, validation, adversarial bundles. |
| `test/live_smoke.sh` | Read-only check against a real systemd host. Not part of the suite. |
| `examples/` | Two synthetic bundles with the report generated from each. |
| `.github/workflows/ci.yml` | ShellCheck, the shell suite, and `unittest discover` on every push. |
| `CHANGELOG.md` | What changed in each version, and why. |

## Tests

```bash
bash test/run_all_tests.sh;
```

The runner regenerates the analyzer fixtures from the collector's own `--fixture`
mode before running, so a change to the bundle format that breaks the analyzer
fails the suite rather than passing against stale fixtures.

Collector tests assert exit codes, expected files, file modes, and the live
property list capturing `Restart`. They also assert the claims this README
makes about behavior: that the source contains no `sudo` and no state-changing
`systemctl` verb, that a live run never labels its own output as a fixture, that
`manifest.sha256` matches the manifest, that verifier output survives an exit
status of 0 with one outcome row and one manifest row, and that a run which lost
artifacts reports itself as partial rather than complete.

Analyzer tests cover the six scenarios, the limited-bundle case, a bundle
whose manifest digests no longer match its files, and drop-in ordering, reset,
and section placement for the declared `Restart=` value.

They also cover the failure paths that matter most: a redaction that fails must
produce an `integrity_error` and retain nothing, an existing output directory
must keep its own mode, the journal boundary must carry an explicit `UTC`
suffix, and the health probe must refuse proxies.

The 59 analyzer tests attack the bundle contract directly, with mutated copies of
a good bundle: a manifest that omits a captured artifact, lists one twice, holds
only a header, or carries a malformed row; an unlisted file added to the
directory; a modified `collection.json` or `command-outcomes.tsv`; a duplicated
or unrecognized outcome row; a manifest exit code that disagrees with the
outcomes; and an altered `manifest.sha256`. Each must be rejected, and a
malformed row must fail cleanly rather than raise. One test walks every fixture
and fails if a report cites an evidence file the bundle did not capture.

A second group proves the trust boundary rather than the parser. A duplicated or
unrecognized outcome row, kept internally consistent with the manifest and its
anchor, must produce zero semantic observations. Health evidence recorded as
`integrity_error`, `unavailable`, `skipped`, or absent from the manifest must not
produce a failing-probe observation. A missing or untrusted `unit-effective.txt`
must not resolve to `Restart=no`. Malformed nested metadata must fail cleanly.
The two committed examples are analyzed and their observation sets asserted, so
they cannot go stale while the suite stays green.

A third group holds the contract to its own wording. An outcome row recorded as
`captured` with no exit status must invalidate the bundle and yield no semantic
observation, because a row that claims a command ran while recording nothing
about how it ended is not evidence that a command ran. A row recorded as
`skipped` or `unavailable` must carry no exit status at all. `collection.json`
is checked field by field against the types and vocabularies the analyzer reads,
with each case rehashed so its bytes still verify, which is what separates an
authentic record from a usable one; a field the schema does not name is left
alone so a later collector can add one. An `--output-dir` inside the bundle is
refused, since reports written there would be files the manifest does not cover.
Every observation the six fixtures can produce is checked against the headings in
`docs/review_guide.md`, so a renamed observation cannot leave a link pointing at
a section that no longer exists. A last test reads the committed examples and
fails if their recorded collector and analyzer versions are not the ones in this
tree, which is what keeps a released version from shipping output an earlier one
produced.

`test/live_smoke.sh` is separate because it needs a real systemd host. The
runner leaves it out.

## Examples

`examples/` holds two synthetic bundles, `evidence_bundle_failed_config` and
`evidence_bundle_ignored_directive`, with the report generated from each. They
are committed so the output format can be read without running anything.

## What the manifest proves, and what it does not

`manifest.tsv` lists every captured artifact **and** the three collection
records (`command-outcomes.tsv`, `collection.json`, `collector-summary.md`) with
SHA-256 digests and byte counts. It cannot contain itself, and it excludes
`manifest.sha256`, which is written afterwards and carries the manifest's own
digest.

Covering the records matters: someone who can edit `collection.json` can change
what the evidence appears to mean without touching a single evidence file.

The analyzer recomputes every digest and additionally rejects a bundle where the
manifest omits a captured artifact, lists one twice, omits a collection record,
disagrees with `command-outcomes.tsv` about a command class or exit code, or
leaves a file in the directory unlisted.

A verified digest says the bytes are the ones collected. It says nothing about
whether they are sensible, so `collection.json` has to pass metadata validation
as well before the report presents its values as fact.

**Content is only read when the contract vouches for it.** Both halves have to
hold: `command-outcomes.tsv` records the artifact as captured, and its manifest
row verified. A malformed outcome record withdraws trust just as a broken
manifest does, because the outcome record is what says the bytes mean anything.

When either half fails, no artifact content is read, the report carries
structural findings only, and every value taken from `collection.json`, identity
and journal access alike, is labelled unverified in the report. `analysis.json`
carries `manifest_valid`, `outcomes_valid`, and `metadata_trusted` so the trust
state is machine-readable rather than something to infer from an error string. A file dropped into a
bundle after collection cannot produce an observation, and neither can an
`integrity_error` row whose artifact was never retained.

That detects accidental change: a truncated copy, a file edited in place, a
transfer that lost bytes, a bundle assembled from two different runs.

It does not detect deliberate alteration. The digests are computed by the same
script that wrote the files, stored in the same directory, and signed by nothing.
Anyone who can edit an artifact can recompute the manifest and the anchor. Treat
the bundle as a careful record, not as tamper-evident evidence. If the bundle
has to survive a hostile reader, record the `manifest.tsv` digest printed on
stdout somewhere the host cannot reach, at the time of collection, and say where
you recorded it.

## Redaction is a filter, not a guarantee

This is narrow, best-effort redaction of selected assignment names. **It is not
a secrets scanner and must not be treated as one.**

It works line by line, with no understanding of systemd's assignment grammar.
A line holding an assignment whose name contains `SECRET`, `TOKEN`, `PASSWORD`,
`PASSWD`, `PASSPHRASE`, `API_KEY`, `APIKEY`, `PRIVATE_KEY`, `CLIENT_SECRET`,
`BEARER`, `JWT`, or `CREDENTIAL`, case-insensitively, is blanked from that
assignment operator to the end of the line. `user:password` pairs inside URLs
are blanked too.

That over-redaction is deliberate. A systemd `Environment=` value can be quoted
and contain spaces, so a value-only rule would blank `TOKEN=abc` and leave
`123"` sitting in the file. A partly redacted secret is worse than a blanked
line, because it looks handled.

What it misses: a secret stored under an unmatched name, an embedded
certificate, a customer identifier, a token inside a JSON body. `--path` reads
metadata only, but `unit-effective.txt` carries whatever the unit file contains.

Read a bundle before it leaves the host.

Read a bundle before it leaves the host. That instruction is part of the tool,
not a disclaimer attached to it.

## Privileges decide how much journal there is

Journal visibility depends on the caller's privileges, group membership, ACLs,
and the host's journal configuration, and it varies by distribution. The
collector records the effective UID and a coarse `journal_access` value: root,
`systemd-journal`, and `adm` are the common full-access cases, and anything else
is recorded as `possibly_restricted`.

That value is a hint, not a proof. The collector cannot establish that a journal
slice is complete, and a persistent journal is a host configuration choice, so
the entries you want may never have been retained. When the value is
`possibly_restricted`, the analyzer says in the report that the slice may omit
entries.

## Exit codes

Collector:

| Code | Meaning |
|---|---|
| 0 | The run completed. Individual command failures are evidence, recorded in `command-outcomes.tsv`, and do not change this. |
| 2 | Usage error: a bad flag or argument. Nothing was collected. |
| 3 | Environment error: a required command is missing, or the output parent could not be created or secured. Nothing was collected. |

A missing command is an environment problem rather than an operator mistake, so
it gets its own exit class instead of being reported as a usage error.

Analyzer:

| Code | Meaning |
|---|---|
| 0 | A report was generated. **This says nothing about whether the bundle validated.** |
| 2 | Usage error, including a bundle path that is missing or unsafe. |
| 3 | The analyzer failed while reading the bundle. |

Validation state lives in the report and in `analysis.json`, deliberately, not
in the analyzer's exit status. Automation should read
`validation.status` rather than `$?`.

`collection_status` in `collection.json` takes one of three values:

| Value | Meaning |
|---|---|
| `complete` | Every command the collector attempted produced its artifact at exit 0. |
| `complete_with_command_failures` | Every artifact exists, but at least one command exited nonzero. |
| `partial` | At least one artifact the collector attempted is absent. |

## A bundle is a window, not a snapshot

The collector runs its commands in sequence, so the machine can change between
any two of them. A process can exit between `systemctl show` reporting its
MainPID and `ps` being asked about it, and the journal can gain entries while
the earlier commands run.

`collection.json` records `journal_since` and `journal_until_utc`. The lower
bound is the expression you passed, evaluated by `journalctl` when it runs; the
upper bound is a fixed timestamp taken at collection start. The end boundary
carries an explicit `UTC` suffix, because systemd reads a bare timestamp in the host's local zone: a
recorded `13:00:00` without the suffix would mean 1 p.m. local while claiming to
be UTC. Read a bundle as a set of observations
made during a bounded window rather than as one atomic snapshot of an instant.

## Live verification

Everything above is exercised against synthetic fixtures, which test the
collector's contract and not a real manager. To check it against a live host:

```bash
bash test/live_smoke.sh ssh.service;
```

It requires systemd as PID 1, collects from the unit you name, verifies every
digest, runs the analyzer, and changes no service state. Name any unit you can
already inspect; a healthy one is fine.

Recorded runs live in the validation matrix below.

## Validation matrix

| Check | Status |
|---|---|
| Fixture suite, six scenarios | Passing |
| Analyzer suite, 59 tests including adversarial bundles | Passing |
| Collector contract and safety tests | Passing |
| ShellCheck at `--severity=style` | Clean |
| Verifier behavior, systemd 255 | Reproduced directly |
| Live collection, systemd 259, unprivileged in `adm` | Passing via `test/live_smoke.sh` |
| Live collection, systemd 259, as root | Passing via `test/live_smoke.sh` |
| Drop-in handling by `systemd-analyze verify`, systemd 255 | Reproduced: a drop-in beside the fragment is read |
| Untrusted-artifact gating (skipped file with a planted signal) | Passing |
| A `.curlrc` cannot redirect the health probe's output | Passing |
| Trust withdrawal on a malformed outcome record | Passing |
| Untrusted health evidence cannot report a failing probe | Passing |
| Missing unit text cannot resolve to `Restart=no` | Passing |
| Committed examples analyze as documented | Passing |
| Live collection with a restricted journal | Not recorded |
| Distributions other than Ubuntu | Not recorded |

The unrecorded rows are unrecorded, not assumed to pass.

## Limits

The collector reads what an unprivileged account can read. A unit whose journal
is restricted, or whose fragment lives somewhere the account cannot reach, will
produce a bundle with those rows failed in `command-outcomes.tsv`. That is the
correct behavior and the report labels it, but it means a bundle collected
without the right group membership answers fewer questions than one collected
with it.

Verification runs against the fragment path. On systemd 255 a drop-in sitting in
the matching `.d` directory beside that fragment is read, and its diagnostics
appear in `unit-verify.txt`; that was reproduced rather than assumed. Behavior on
other releases is untested.

`systemd-analyze verify` checks a unit description. It does not establish what
the running manager is currently applying, which is why `systemctl show` sits
beside it in every bundle. `systemctl cat` likewise shows the files on disk, and
those can differ from what the manager holds when a unit changed without a daemon
reload.

Bundles from collector 0.1.0 carry no `Restart` property. The analyzer skips the
restart comparison for them rather than guessing.

The analyzer parses `systemctl show` key-value output. A unit property that
systemd renames or reformats in a future release will stop being parsed, and the
report will show fewer state facts rather than wrong ones.

The analyzer accepts collection schema 1.2 only. Earlier bundles kept the
collection records outside the manifest, so the current coverage rules would
reject them; declaring support for a schema this validator rejects would be a
promise the code breaks. Collect again rather than analyzing an old bundle.

Only `.service` units are supported. Sockets, timers, mounts, and targets are
out of scope, and the collector rejects a unit name that does not end in
`.service`.

The URL you pass to `--health-url` is written into `collection.json` verbatim, as
`inputs.health_target`. That is deliberate, because a reader cannot judge a
health artifact without knowing what was probed, but it means the hostname, port,
and path travel with the bundle. Redaction does not touch it; the redaction
filter blanks credential-shaped assignments in captured output, not metadata this
collector recorded about its own inputs. If the endpoint itself names something
you do not want to share, treat the bundle as carrying that name and edit the
field before handing the bundle on, then regenerate `manifest.tsv` and
`manifest.sha256` so the bundle still verifies.

## License

Copyright 2026 Joseph Tracy. All rights reserved. The source is published for
reading and review. It carries no license to use, copy, modify, or redistribute
it, so treat it as a reference implementation rather than a dependency. Ask if
you want different terms.
