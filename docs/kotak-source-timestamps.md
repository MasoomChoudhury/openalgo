# Kotak SFeed source timestamps: repair and rollout

This patch starts from upstream commit `3bed8966bcc20bb4f046ab019e9ff3fa276ccf16` (version label 2.0.2.6). That is an inspected development baseline, **not a claim about the currently deployed container**. Keep the previous image and databases backed up. Pin the repaired Git commit or built image digest when deploying.

## Contract

Existing prices, OHLC, volumes, depth, topics and modes remain available. New fields are additive:

| Field | Meaning |
| --- | --- |
| `time_schema_version` | `1` |
| `adapter_revision` | SHA-256 fingerprint of the seven actual adapter/proxy source files, prefixed `kotak-sfeed-time-v1:` |
| `broker_price_time`, `broker_last_trade_time` | UTC ISO time decoded from SFeed `last_trade_time`, or null |
| `broker_quote_time` | UTC ISO time decoded from SFeed `last_update_time`, or null |
| `broker_depth_time` | UTC ISO `last_update_time` belonging to the full depth snapshot, or null |
| `source_time_raw` | Original decoded integers per price/quote/depth component; unavailable/sentinel values retained |
| `source_time_unit` | `unix_seconds`; no magnitude guessing or price-divider scaling |
| `source_connection_state` | `connected` or `disconnected`; a broker disconnect invalidates clocks even while the proxy stays connected |
| `server_received_at` | Receipt time of the relevant decoded packet; does not prove provider freshness |
| `component_received_at` | Receipt times saved separately with price, quote and depth |
| `server_published_at` | Current publication time; can advance for cached data |
| `timestamp`, `timestamp_origin` | Legacy server milliseconds, explicitly labelled `server_publication` |
| `ltt`, `ltt_origin` | Broker last-trade milliseconds when present, otherwise legacy publication time with explicit provenance |

The decoder preserves the original negative `last_update_time` before normalizing it. Zero, negative, missing, wrong-unit, boolean and non-integer clocks become null. Positive seconds outside 2000–2100 become null. Plausible future clocks are preserved as source evidence and rejected by consumers.

Prices and books own their clocks. A lite price update never refreshes a cached book. A book update never moves the last-trade clock forward. Known older updates are ignored per component; an update with no clock remains unknown rather than borrowing the old clock. Disconnect, reconnect and unsubscribe clear their component caches. Broker-close notices immediately null the clocks of previously published instruments, even if clients remain connected to the proxy. These notices preserve display values and bypass MarketDataService pricing so they cannot cause sandbox/RMS fills. Callbacks from a replaced broker client and late packets while disconnected are ignored. Repeated snapshots retain the old source/receipt clocks. Empty or zero full-depth levels replace old levels. Legacy HSM normalization is retained and is not certified by this SFeed contract. Replaced-client callback fencing applies to both transports.

A recent last-trade timestamp is not proof of a recent book. A book can be current while LTP is old because no trade occurred. Index `last_trade_time` must be verified to represent the index update being used; an index has no invented depth clock. Null or unsuitable index clocks legitimately keep strict consumers blocked.

## Before deployment

1. Pause execution-plane entries, reconcile outstanding exposure and preserve broker-held protection. Stop its app before running database-owning CLI commands; closing the dashboard is insufficient.
2. Record the current OpenAlgo repository, commit/image digest, broker login and WebSocket routing. Back up its persistent volumes and the execution database. This patch does not require an OpenAlgo database migration.
3. Compare this branch with the actual deployed source. If the deployment contains additional Kotak changes, port this focused patch onto that source and rerun the tests; do not discard those changes by assuming the shared version label identifies identical code.
4. Deploy the repaired pinned image to both the REST application and its WebSocket process where they share source. Restart the WebSocket process: the revision is computed once per process. Keep the previous image available for rollback. Public WSS routes to the proxy port (normally 8765), not Flask port 5000.
5. Deploy the paired execution-plane client changes. Old timestamp certificates cannot enable this Kotak contract. No broker order or automatic mode change is needed for data validation.

## Capture and verify source semantics

Temporarily set **`KOTAK_TIME_DIAGNOSTICS=true` on the OpenAlgo/WebSocket service**. It emits allowlisted time-only `KOTAK_TIME_AUDIT` records at `decoded_broker_packet` and `adapter_publication` boundaries, sampled once per instrument/boundary per 30 seconds (256-key bound). It never logs auth/control frames. Use these focused logs rather than enabling global debug logging with account payloads.

From the paired execution plane, capture NIFTY Quote mode 2 plus an **actual current option** Depth mode 3 using `inspect-feed --symbol ACTUAL_SYMBOL --capture-file /data/diagnostics/unique.json --seconds 30`. The capture includes raw received market-data JSON and client receipt clocks, redacts configured secrets, excludes auth messages, and sends no orders. Use a new filename each time.

Compare the decoded integer, normalized component clock, adapter publication and client packet for:

- Active market index updates, option trades and full book changes. Confirm epoch, seconds, UTC conversion and timestamp meaning against broker documentation and observed changes.
- A quiet option, unchanged/repeated packets and after-close snapshots. Fresh publication/receipt must not advance the provider clock. Do not expect after-close packets to pass a 60-second freshness limit.
- Lite LTP updates, book-only updates, missing/sentinel fields and each subscribed mode.
- Reconnect/resubscribe and multi-connection pooling: no pre-disconnect component may be reused. Revision/schema/raw fields must survive delivery.

Reference: [Kotak official SFeed guide](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/docs/guides/websocket.md) and [decoder](https://github.com/Kotak-Neo/kotak-neo-python/blob/main/neo_api_client/websocket/feed/protocol.py). The guide's negative sentinel represents 1900-01-01 IST; it is not a current quote. Documentation and passing replay tests **cannot establish the live feed's semantics by themselves**.

If `last_update_time` or the index clock is absent or semantically unsuitable, leave it unknown. Repair the upstream preservation path or use another verified feed; do not substitute `time.time()`, REST retrieval time, `ltt` fallback, or publication time.

## Export actual deployed evidence

After reviewing captures and deployed source, run **inside the running OpenAlgo image**, using its own Python:

```sh
python scripts/export_kotak_timestamp_audit.py \
  --output-dir /tmp/kotak-audit-UNIQUE \
  --server-version ACTUAL_DEPLOYED_COMMIT_OR_IMAGE_DIGEST \
  --reviewed-by REVIEWER \
  --explanation 'Actual observed clock units/meaning, index/depth modes, cache/reconnect and capture references'
```

The directory contains the seven deployed files and `audit.json`. The utility copies and hashes evidence; it does not automatically verify semantics or issue a trading certificate. Copy the directory through your normal private admin channel, mount it read-only into the stopped execution plane, and run its `audit-feed --audit-file /evidence/audit.json` with the same persistent execution database and account environment. That command validates every file hash and the emitted revision and records the operator review. Never use development-checkout or synthetic-test evidence to certify production.

Turn the diagnostics flag off after capture. Do not include API keys, tokens, cookies, complete environment files or account credentials in evidence.

## Acceptance and rollback

Run local replay/regression tests before building:

```sh
LOG_FORMAT='%(levelname)s %(message)s' LOG_COLORS=false uv run --no-dev pytest test/test_kotak_source_times.py test/test_kotak_sfeed_protocol.py test/test_kotak_sfeed_client.py test/test_kotak_index_depth_tick.py test/test_kotak_index_feed_subscription.py
```

Check REST compatibility, OpenAlgo option-chain UI, WebSocket Quote/Depth delivery and execution-plane freshness displays. On the next active session, confirm source age and packet evidence, then deliberately exercise every enabled strategy's Analyzer protection lifecycle/trigger/recovery through the normal execution system. A timestamp audit is separate from protection verification and never certifies live trading. No application code switches OpenAlgo's Analyzer/live mode.

If clocks, prices, book levels or source revisions disagree: keep entries paused, record the incident, invalidate the execution-plane audit and restore the previous pinned image. After a rollback the revision mismatch blocks new entries automatically. Existing protection and necessary exits must continue through the execution system. Revalidate source and reconcile before resuming.
