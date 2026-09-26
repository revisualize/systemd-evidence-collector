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
| **Computed manifest.tsv sha256** | `7363aa7b87c912ec73bdcef052f5e4ad9db3e7c6850265c48603ad9eeb56adf2` |
| **Validation status** | **valid_with_limitations** |

> This report is evidence-linked and observational, with bounded review guidance. It identifies no root cause and authorizes no restart, reload, reset, or other state-changing action.

## Parsed State Facts

| Property | Captured value |
|---|---|
| `LoadState` | `loaded` |
| `ActiveState` | `failed` |
| `SubState` | `failed` |
| `Result` | `exit-code` |
| `MainPID` | `0` |
| `ExecMainCode` | `1` |
| `ExecMainStatus` | `1` |
| `NRestarts` | `0` |
| `Restart` | `on-failure` |
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

### OBS-CONFIG-LIKE (review)

The journal contains a configuration-like signal. Confirm the current configuration against a known-good baseline before considering service recovery.

**Evidence:** `journal.txt`, `systemctl-show.txt`  
**Next step:** docs/review_guide.md#configuration-signals

## Next Review Step

Work the referenced section of docs/review_guide.md in the repository that produced this report. Preserve the bundle, verify scope and safety, and treat this report as review input rather than an execution instruction.
