# Unattended startup: local verification

Verified: 2026-10-06 IST. This records local implementation checks, not a
deployed release certificate or an unattended trading activation.

## Identities

| Component | Identity |
|---|---|
| Execution Plane source | `57fb6446bf8075d68fa8e4b6820c24f29508f10565791d4a7c0e11a80bfc1f5a` |
| OpenAlgo automation source | `265247228a6ed9ee3393b82e133a9f60acee04e07c35ac8e8054b0b7d8ede360` |
| Execution Plane SQLite | Schema 6 |
| Deployment / protection / execution scopes | 3 / 4 / 5 |
| Automation HTTP contract | 1 |
| Account and host | `masoom`; same Dokploy host, confirmed by operator |

These are source digests of the current local files. Actual running container
identities must still be retained after deployment. Existing acceptance bundles
do not certify these changes.

## Results

| Check | Result |
|---|---|
| Execution Plane full Python suite | 381 passed |
| Kotak automation, token-format, callback and session-resume suites | 92 passed |
| Dashboard TypeScript and production build | Passed |
| Desktop/mobile dashboard, keyboard history, errors, single-slot display and report downloads | Passed |
| Both base Compose configurations with built-in private-network attachments | Valid |
| Isolated container environment handling | Leading-zero MPIN and literal `$`/`#` preserved; shared secret unchanged |
| Both repository whitespace checks | Passed |

Coverage includes daily uniqueness, pre-open waiting, missed windows, calendar
failure, restart reuse, signed account-scoped requests, replay rejection, clock
skew, valid-token reuse, TOTP/MPIN renewal, bounded rate-limit attempts, ambiguous
login fencing, immutable authentication events, stale browser-cookie expiry,
management revisions across restart, source-identity changes, valid-token contract
refresh, exposure-aware startup renewal, protection coverage and
authenticated/origin-checked dashboard controls. Existing execution regressions
remain part of the full Execution Plane suite.

One existing FastAPI/Starlette deprecation warning remains. OpenAlgo's tests also
emit existing Colorama logging errors during process-exit cleanup; the test
results themselves pass.

## Reproduce

Execution Plane:

```bash
.venv/bin/python -m pytest -q
cd dashboard
npm run build
```

From the Execution Plane root, run `tests/dashboard_reassessment_smoke.cjs` with
Playwright available to Node and the existing Chrome executable.

OpenAlgo fork:

```bash
LOG_FORMAT='%(message)s' .venv/bin/python -m pytest -q \
  test/test_kotak_automation.py test/test_kotak_auth_token_format.py \
  test/test_broker_callback_logging.py test/test_auth_resume.py
```

## Pending deployed observations

Configure credentials privately, attach the private network, deploy both builds
with the private credentials, then verify the manual private service invocation. Retain a morning
shadow-startup observation and Analyzer exposure-recovery evidence before
unattended activation. See `UNATTENDED_STARTUP.md` and `pending tasks.md`.

No real broker login, trading session, order or production mutation was performed
by this verification. The Docker environment probe had networking disabled,
no persistent volumes and only fake fixture values. Daily startup is enabled by operator request;
OpenAlgo mode and strategy release gates remain under their existing controls.
