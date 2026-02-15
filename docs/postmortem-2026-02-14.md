# Postmortem: Antigravity 403 ToS Account Ban Incident (2026-02-14)

## Summary

On 2026-02-14, two Antigravity credentials were disabled upstream by Google with `403 PERMISSION_DENIED` and the message:

`This service has been disabled in this account for violation of Terms of Service.`

Affected credentials:

- `ag_spherical-mechanism-90r0m-1766820543.json`
- `ag_radiant-port-sss4b-1768200922.json`

Impact:

- Increased failed retries and noisy logs during the incident window.
- Repeated attempts against already-banned credentials before hard-lock controls were added.


## Timeline (ICT, UTC+7)

- `09:12:13` first confirmed ToS-ban `403` on `ag_spherical-mechanism-90r0m-1766820543.json`.
- `09:12:14` first confirmed ToS-ban `403` on `ag_radiant-port-sss4b-1768200922.json`.
- `09:12:13` to `09:12:41` repeated stream failures on both credentials while retry/rotation was active.
- Post-window logs contain repeated ToS message lines, confirming continued attempts on already-disabled upstream accounts.

Observed in logs during early incident window:

- `PERMISSION_DENIED` with explicit ToS disablement text.
- Endpoint fallback and quota pressure signals (`429` and cooldown parsing) in nearby traffic.


## Root Cause

Primary root cause:

- Upstream account-level disablement by Google (ToS violation), returned as `403`.

Local contributing failure mode:

- Credentials that had already received ToS `403` were not treated as permanently non-recoverable.
- Some paths could re-enable or continue selecting credentials that should have been permanently excluded.


## Contributing Factors

- High request volume and retry/fallback activity around the same period increased failure amplification.
- Existing auto-ban behavior did not enforce an irreversible state for ToS `403`.
- Forensic visibility was previously fragmented (now improved with request audit trails).


## What We Changed

### 1) Added structured request audit logging

Commit: `91f5274`

- Added `request_audit` pipeline and endpoints:
  - `/api/audit/stats`
  - `/api/audit/incidents`
  - `/api/audit/timeline`
  - `/api/audit/recent`
- Captures per-attempt status, credential, model, outcome, ban/validation signals.

### 2) Enforced permanent hard lock for 403

Commit: `ee392d6`

- `403` always triggers auto-ban.
- Ban reason is stamped with `Hard-locked:` prefix.
- Re-enable attempts are rejected for hard-locked credentials.


## Current State

Both incident credentials are now hard-locked in DB:

- `ag_spherical-mechanism-90r0m-1766820543.json`
  - `disabled=1`
  - `disabled_reason="Hard-locked: Auto-banned: HTTP 403"`
- `ag_radiant-port-sss4b-1768200922.json`
  - `disabled=1`
  - `disabled_reason="Hard-locked: Auto-banned: HTTP 403"`


## Lessons Learned

- Treat ToS/account-disable `403` as terminal, not retriable.
- Ensure disablement semantics are monotonic for severe account-level failures.
- Keep forensic telemetry first-class so incident triage can be data-driven.


## Follow-up Actions

- Add dashboard surfacing for hard-locked credentials and ban reason categories.
- Add alerting on first ToS-ban signal per credential (not only aggregated failures).
- Add regression tests for:
  - `403 -> hard-lock`
  - hard-locked credential cannot be re-enabled
  - selection logic excludes hard-locked credentials.
