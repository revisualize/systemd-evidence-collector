# Review guide

Every observation in an analysis report points at a section here. The guide is
deliberately short: it tells a reviewer what to read next and where to stop. It
prescribes no fix, because the bundle cannot tell you whether a fix is safe.

Rules that apply to every section:

- Read the bundle before touching the host. The bundle is the state as it was;
  the host has moved on.
- Check `command-outcomes.tsv` first. An empty artifact from a failed command
  and an empty artifact from a quiet system look identical.
- An observation is a pointer, not a finding. Two observations firing together
  is not a cause.
- Stop and escalate rather than widening access, clearing state, or restarting
  to see what happens.

## Failed unit

The unit reported a failed state.

1. Read `Result`, `ExecMainCode`, and `ExecMainStatus` in `systemctl-show.txt`.
   `exit-code` sends you to the application; `signal` sends you to the kill
   source; `timeout` sends you to startup behavior and unit timeouts.
2. Read the journal window in `journal.txt` from the first failure forward, not
   from the end backward.
3. Compare `unit-effective.txt` against the configuration you expect.

Stop when you can state what the unit did in one sentence that cites a file.

## Active but unhealthy

The manager reports the unit active while the health probe did not succeed.

1. Read `inputs.health_target` in `collection.json`, which records the URL the
   operator supplied. A probe against the wrong port is evidence about the
   probe, not about the service.
2. Read `health.stderr`. A connection refusal, a timeout, and an HTTP error are
   three different findings.
3. Check `process.txt` for a live main process.

An active unit with a failing probe shows that manager state and the probe
result disagree. Keep process state, readiness, dependencies, and probe
targeting as separate questions.

## Configuration signals

The evidence contains a configuration-like signal, or the verifier printed
diagnostics, or the declared restart policy differs from the manager's.

1. Read `unit-verify.txt` if it is present. Diagnostics at exit 0 are still
   diagnostics: a directive named there was parsed and discarded.
2. Compare `unit-effective.txt` with `systemctl-show.txt`. The first is what the
   files say. The second is what the manager holds. They disagree when a unit
   changed without a daemon reload, or when a directive failed to parse.
3. Note that `systemd-analyze verify` checks a unit description. It does not
   establish what the running manager is currently applying.

## Access signals

The evidence contains an access-related signal.

1. Read the service identity: `User`, `Group`, and `WorkingDirectory` in
   `systemctl-show.txt`.
2. Read `path-metadata.txt` if a path was supplied. Ownership and mode along the
   whole path matter, not just the leaf.
3. Consider the sandboxing directives in `unit-effective.txt`
   (`ProtectSystem`, `ReadWritePaths`, and similar) before concluding that file
   permissions are wrong.

Do not broaden permissions to make an error disappear. A permission that is too
wide is a second incident.

## Dependency signals

The evidence contains a dependency or connectivity signal.

1. Establish which side failed. A refused connection is evidence about the
   target; a timeout is evidence about the path.
2. Check the dependency's own state before retrying this unit.
3. Read the ordering and requirement directives in `unit-effective.txt`. A unit
   that starts before its dependency is ready fails in a way that looks like a
   dependency outage.

## Restart context

Restart-related context is present: a restart count, a restart policy, or both.

1. Read `NRestarts` and `Restart` in `systemctl-show.txt`. A unit that restarted
   repeatedly has overwritten its own symptom.
2. Read the journal window for the earliest failure, not the most recent one.
3. Stop conditions: do not reset the failure counter, do not restart, and do not
   clear state until you can name what the earliest failure was.

## Limited bundle

Something narrows what this bundle can answer. That covers three different
situations, and the report's validation section says which one you have.

1. **Validation errors.** The manifest disagrees with the bundle's contents, or
   the outcome record is malformed. A checksum mismatch and a missing manifest
   row are different problems; read the errors individually. Nothing in the
   bundle is trustworthy until you know which.
2. **Missing evidence.** An artifact the collector attempted is absent, recorded
   as `unavailable` or `integrity_error`. Collect again if the host still holds
   the evidence, and record why the first attempt failed.
3. **Limitations only.** The evidence is present and verified, but something
   bounds it: a restricted journal, a command that exited nonzero, a collection
   window that may not cover the incident. The bundle is usable. Read it knowing
   what it does not cover.

In all three cases, absence of a signal here is not evidence that the signal was
absent on the host.

## No rule triggered

No observation rule matched.

This means the analyzer found nothing it is built to notice. It does not mean
the unit is healthy, and it does not mean the evidence is uninteresting.

1. Read `collector-summary.md` and `systemctl-show.txt` yourself.
2. Check the limitations section of the report. A restricted journal or a failed
   command explains many quiet bundles.
3. If the bundle is genuinely clean and the incident is real, the evidence you
   need was not in this window. Widen `--since` and collect again.
