# Kotak margin integrity and capability repair

Status: implemented and verified in isolation; deployment follows the ADX observation gate in the execution-plane repository.

The adapter now rejects failed transformations, missing/nonfinite/negative amounts, invalid HTTP/broker status and incomplete aggregates. A failed leg cannot become a success-labelled partial sum. API success, RMS approval and insufficient funds are retained separately. The service checks body status as well as HTTP status.

Complete calculations expose `data.calculation` version 1 with method, scope, amount semantics, completeness, request/result counts, canonical SHA-256 request identity, measurement time and sanitized per-leg outcomes. The current method is `standalone_order_sum`, scope `independent_orders`, semantics `sum_of_net_order_requirements`, `verification_supported=false`. These are diagnostic estimates, not verified portfolio margin. No undocumented basket endpoint or hedge relief is enabled.

Kotak's documented fields distinguish `ordMrgn`, `reqdMrgn`, `mrgnUsd`, `totMrgnUsd`, `insufFund` and `rmsVldtd`. This release preserves the meaning of `reqdMrgn` as a single-order net requirement and does not sum it into an authoritative account/portfolio requirement.

The coordinated execution-plane consumer requires a complete current quote with matching request identity and explicit supported joint-portfolio semantics for short exposure. The current Kotak capability cannot satisfy that contract, so short baskets remain unavailable with an explicit reason. Long premium-only strategies retain their existing checks. Defined maximum loss is not substituted for broker margin.

Focused adapter tests: 25 passed. An earlier 20-case set plus five Dhan/service compatibility checks passed with network disabled, dummy settings and no application mounts. The full repaired repository run passed 5,689 tests and retained 20 failures and three setup errors; a full unmodified baseline at 6955d13 reproduced the exact same 23 failing/error case identities, with zero introduced failures. The full repository suite is not green. Exit logging also retains an existing closed-stream warning.

Build/deployment must overlay only the three verified modified installed files onto the frozen actual production image, avoiding unrelated checkout changes. Preserve the original image identity and every unaffected file. Install the strict execution consumer first, replace OpenAlgo only while reconciled/flat with terminal orders and verified backups, and verify unchanged streaming adapter bytes and audited clock identity afterward. No trading or authentication exercises are part of verification.
