# Unattended startup and Kotak authentication

Implemented in both repositories; daily startup is enabled by default by operator request. No credentials belong in Git,
diagnostic exports, or chat. Neither application changes OpenAlgo's mode.

## Private deployment configuration

OpenAlgo Dokploy environment (set values privately):

```dotenv
KOTAK_AUTOLOGIN_ENABLED=true
KOTAK_AUTOLOGIN_USERNAME=masoom
KOTAK_MOBILE_NUMBER=+91YOUR_REGISTERED_NUMBER
KOTAK_MPIN=YOUR_SIX_DIGIT_MPIN
KOTAK_TOTP_SECRET=YOUR_KOTAK_API_BASE32_SEED
OPENALGO_AUTOMATION_SECRET=YOUR_RANDOM_64_HEX_CHARACTER_SECRET
BROKER_API_KEY=YOUR_KOTAK_UCC
BROKER_API_SECRET=YOUR_KOTAK_API_ACCESS_TOKEN
```

Confirmed deployment: OpenAlgo username `masoom`; both applications run on the
same Dokploy host. The seed is for Kotak's API
TOTP enrolment, **not** OpenAlgo's website authenticator. Six-digit codes cannot
replace the seed. Keep MPIN values as strings, including leading zeros. In a
Compose `.env`, single-quote secrets containing `$` or `#`; Compose passes the
resolved value into the container. Do not double the `$` in the resulting
container value. OpenAlgo now gives container environment values precedence over
its mounted `.env`. Its Compose file explicitly passes the new credentials and
existing broker key/secret. Do not configure actual secrets in `.sample.env`.

Generate a shared secret locally, then put its value privately in both deployments:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Execution Plane Dokploy environment:

```dotenv
OPENALGO_AUTOMATION_URL=http://openalgo-automation:5000
OPENALGO_AUTOMATION_SECRET=THE_SAME_SECRET_AS_OPENALGO
```

No website username/password/TOTP is used by Execution Plane. Browser login
continues normally. API keys, model credentials and dashboard auth settings
remain configured as before.

Both deployments are on the same Docker host. **Deploy Execution Plane first**:
its base Compose file creates the internal `openalgo-automation` Docker network.
Then deploy OpenAlgo using its base `docker-compose.yaml`; it joins that existing
network with the `openalgo-automation` DNS alias. No overlay or public automation
domain is needed. The dashboard authentication container and Traefik stay outside
this network. OpenAlgo retains its normal network for outbound broker access.

If your Dokploy OpenAlgo deployment uses an Application rather than Compose,
attach its container to the existing `openalgo-automation` network and assign the
same alias through the deployment network settings. Updating only its Docker
image does not add that network attachment. Check this before leaving startup
unattended. Preserve all current database volumes and domain/port mappings.

Persistent database volumes and reliable outbound connectivity are required.
Keep the host clock synchronized with NTP.

## Activation

1. Configure the private credentials and matching shared secret in Dokploy.
   Startup is enabled in the shipped configuration; OpenAlgo Compose defaults to
   `KOTAK_AUTOLOGIN_ENABLED=true` and username `masoom`. You can explicitly set it
   false to disable the service, or set `daily_startup.enabled: false` to disable
   the client. Existing explicit false environment values must be changed to true.
2. Deploy Execution Plane first, then OpenAlgo, preserving database volumes.
   Confirm both are healthy and the private service is reachable. Missing
   automation settings show `Configuration required` in Execution Plane; they do
   not crash its dashboard or protection monitor. Environment changes require a
   container restart; credentials are never sent from the dashboard.
3. Keep OpenAlgo already in Analyzer mode. The service probes real Kotak limits,
   rather than Analyzer virtual funds. The first scheduled morning is a shadow
   observation until existing execution release proof passes.
4. Both apps must remain running before 08:55 IST. The workflow validates the
   regular exchange calendar, prepares authentication/contracts, waits for fresh
   market-open source clocks, and starts by 09:30. First review remains 09:32.
   No browser login or manual Start action is needed for a successful daily start.
5. Retain morning startup and Analyzer exposure-recovery evidence. This release
   follows the operator's request to enable preparation by default; it does not
   certify unattended recovery or release trading paths. Existing risk and
   protection/reassessment gates continue to withhold uncertified entries.

Changes to execution/configuration identities invalidate earlier certification;
retain matching evidence for the new deployed build. Startup readiness is not
strategy release or a promise of an entry. Genuine signals and all risk,
liquidity, freshness and protection gates still apply.

The private status response includes a versioned digest of the running OpenAlgo
authentication, token-storage, browser-session and environment-loading code.
Execution Plane retains it in its release scope when daily startup is enabled.
An identity change during a session blocks entries and invalidates pending
proposals. Protection verification is now version 4, execution scope version 5,
and deployment scope version 3; earlier evidence cannot certify this writer.

Startup still requires current audited NIFTY and option measurement clocks. If
the deployed timestamp certificate is missing, or the feed does not meet the
existing freshness contract, preparation will report the blocker and automatic
session creation will wait until 09:30, then require manual action. Enabling the
scheduler does not certify that missing evidence or guarantee a session start.

## Recovery and supervision

Overview shows state, stage, broker authentication, contracts, reason, daily
history and the diagnostic trace. Authenticated APIs:

- `GET /api/startup`: current configuration/status without secrets.
- `POST /api/startup/skip`: suppress today's automatic startup; does not close exposure.
- `POST /api/startup/retry`: clear suppression/retry only within the morning window,
  before a daily session was started. It never bypasses uncertain broker login.

Existing Pause/Stop persist operator suppression. Existing Resume is required
for a paused active session; startup recovery never overrides it. Restarts reuse
an existing daily session without replaying missed reviews. Next-day startup
requires prior exposure and owned outstanding orders to be reconciled/flat.

Authentication requests use HMAC-SHA256 over method, exact path, timestamp,
nonce, body hash and the existing OpenAlgo API-key hash. The service also verifies
that this API key belongs to the configured OpenAlgo user. Nonces are retained in OpenAlgo's existing database.
Requests older/newer than 30 seconds or reused nonces are rejected. The service
accepts only `operation_id`, scopes credentials to one configured user and
returns no broker token. Valid tokens are reused without feed teardown. Stale
master contracts can be refreshed through a separate saved operation without
logging in again. A contract-only operation never renews an expired token; that
requires the normal authentication fence.

Authentication has at most three attempts for explicit transient broker
responses. Rejected credentials stop immediately. An ambiguous timeout,
interrupted operation or unconfirmed token blocks further automatic login;
inspect status and authenticate manually in OpenAlgo to resolve it. Persisted
operation IDs prevent retrying uncertain requests after restart.

The position monitor stays scheduled during renewal, reporting degraded
connectivity. Order submissions are fenced during token renewal; pending entries
and stale management baselines are invalidated. After login, necessary recovery
and exits are permitted before discretionary work. No client can confirm broker
fills or issue an exit while broker access is unavailable. Keep broker-resident
protection in place.

## Local verification

The Execution Plane schema is now version 6 with unique account/day startup rows
and immutable event history. Evaluation remains compatible with versions 1–6.
New startup/client code is included in the release source digest. No new service
or database is needed. Credentials have not been populated locally and no broker login,
trading session, order or production activation was performed for implementation.

Run `python -m pytest -q` and the dashboard production build in Execution Plane;
run the Kotak automation/token-format/callback/session-resume fixtures in the
OpenAlgo fork. These are offline proof; market-hour startup, clock verification
and exposure-recovery observations remain deployment acceptance tasks.

Recorded results and local source identities: `UNATTENDED_STARTUP_VERIFICATION.md`.
