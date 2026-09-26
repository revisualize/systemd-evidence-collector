# Evidence Bundle Human-Review Report

## Scope

| Field | Value |
|---|---|
| **Unit** | `example-api.service` |
| **Collected at (UTC)** | `20260925T015333Z` |
| **Collector version** | `0.11.0` |
| **Analyzer version** | `0.11.0` |
| **Collection mode** | `synthetic_fixture` |
| **Journal access** | `not_applicable_fixture` |
| **Computed manifest.tsv sha256** | `cb3cb5a465ba0b40041352d76e619314581884b6f95baa003fe6493dd2facce5` |
| **Validation status** | **valid_with_limitations** |

> This report is evidence-linked and observational, with bounded review guidance. It identifies no root cause and authorizes no restart, reload, reset, or other state-changing action.

## Parsed State Facts

| Property | Captured value |
|---|---|
| `LoadState` | `loaded` |
| `ActiveState` | `failed` |
| `SubState` | `failed` |
| `Result` | `signal` |
| `MainPID` | `0` |
| `ExecMainCode` | `2` |
| `ExecMainStatus` | `9` |
| `NRestarts` | `0` |
| `Restart` | `no` |
| `RestartUSec` | `100ms` |
| `User` | `example-api` |
| `Group` | `example-api` |
| `WorkingDirectory` | `/opt/example-api` |
| `FragmentPath` | `/etc/systemd/system/example-api.service` |

## Validation

### Limitations

- health.txt was written by a command that exited 22. Read it as the output of a failed command rather than as a complete answer.
- health.stderr was written by a command that exited 22. Read it as the output of a failed command rather than as a complete answer.

## Observations Requiring Human Review

### OBS-UNIT-FAILED (review)

The unit is reported as failed in the captured state evidence. Review Result, ExecMain fields, and the bounded journal window before considering a recovery action.

**Evidence:** `systemctl-show.txt`, `systemctl-status.txt`  
**Next step:** docs/review_guide.md#failed-unit

### OBS-UNIT-VERIFY-DIAGNOSTICS (review)

systemd-analyze verify produced diagnostic output (exit 0). Exit 0 with diagnostics does not mean the unit is clean; a directive named in the output may have been ignored.

**Evidence:** `unit-verify.txt`, `command-outcomes.tsv`  
**Next step:** docs/review_guide.md#configuration-signals

### OBS-RESTART-DECLARED-DIFFERS (review)

The captured unit text carries Restart=on-failuer, but the manager reports Restart=no. The declared restart policy may not be the one in effect; compare the unit text, drop-ins, and any verifier output before relying on automatic restart.

**Evidence:** `unit-effective.txt`, `systemctl-show.txt`, `unit-verify.txt`  
**Next step:** docs/review_guide.md#configuration-signals

## Next Review Step

Work the referenced section of docs/review_guide.md in the repository that produced this report. Preserve the bundle, verify scope and safety, and treat this report as review input rather than an execution instruction.
