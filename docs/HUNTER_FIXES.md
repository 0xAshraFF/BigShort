# Hunter v2: execution and budget fixes

Scope: only `bigshort/hunter.py`, new hunter-only modules, tests and this documentation. The incumbent's core, runner, market adapter, CLI, Dockerfile and Compose configuration are unchanged. Neither running container was accessed or restarted during development. No key or paid model call was used for testing.

## Safe update

From the existing checkout on `codex/model-hunter`:

```bash
git pull --ff-only origin codex/model-hunter
docker compose up -d --no-deps --build hunter
docker compose logs -f --tail 100 hunter
```

The command targets the hunter service only. Do not run `down -v`. The named hunter volume preserves its cash, positions, calls, model IDs and original start time. Before rebuilding, an optional SQLite-consistent backup can be made while the old hunter is running:

```bash
docker compose exec -T hunter python - <<'PY'
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
folder=Path('/hunter/backups')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
folder.mkdir(parents=True,exist_ok=False)
for path in Path('/hunter').glob('*.sqlite'):
    with sqlite3.connect(path) as source, sqlite3.connect(folder/path.name) as target:
        source.backup(target)
print('Hunter database backup complete')
PY
```

## Execution semantics

New entries use a validated quote fetched **after** the model response, timestamped at actual observation. The initial stop remains tied to the proposed snapshot, and is rejected if no longer valid; snapshots/decisions expire after 180 seconds. A move over 0.5% from the proposed snapshot rejects entry. These are explicit signal-validity controls, not a change of strategy or model.

Position management observes fresh executable asks, independently of asynchronous model requests. Historical candle highs never fabricate fills before entry. Stops and profit locks use observed quotes; they are **not** exchange-hosted orders or guaranteed tick-perfect fills. During a market-data outage no actual execution can be simulated; the next available quote determines the exit, including gap slippage. `management_observation` records observation gaps and processing timestamps. Large gaps make performance evidence weaker even when accounting is correct.

Kill-file `/hunter/STOP` and expiry set and save a permanent entry halt before network work. When a fresh quote becomes available they flatten simulated exposure. Funding failures cannot block protective quote processing: funding is retried separately, using recorded exposure at each settlement boundary. Later funding adjustments are included in closed-trade reports exactly once. Reports flag unsettled closed trades. This can revise a provisional leaderboard without rewriting old events.

A legacy open position keeps its original size, stop, fees and cash. Future management uses v2 quote semantics; earlier simulated fills are not retroactively rewritten. The scorecard exposes `execution_version=2` at runtime, and a migration event identifies that boundary. Results spanning that change are not a clean fixed-engine experiment. For a legacy partial position, exposure is reconstructed from its execution journal and checked against the current quantity. An inconsistent journal blocks startup rather than inventing funding exposure.

## Deadline and decisions

The 24-hour deadline is persisted and anchored to the first recorded model call, including the original calls before upgrade. Restarting does not start a new round. If already expired, v2 makes no new model requests, halts new entries and flattens existing simulated positions at the next fresh quote. It continues servicing accounting and reporting. There is no automatic promotion or real-money execution.

Candidate models remain the original free Scout, DeepSeek Analyst and Opus Elite. This patch does not silently switch Opus to GLM or add long trading. All three still have the same short-only instructions. The 30-minute request schedule is durable. Outstanding results cannot execute after expiry, after position identity changes, during a management/accounting error, or after a kill switch. Models see one common pre-round peer snapshot; pending peer decisions are excluded.

## Spending

- Hard local allocation: free Scout $0, Analyst at most $3, Elite at most $3. The free slot's unused $3 is not transferred. Global guard $9; the user's $10 remains the absolute ceiling.
- Existing calls count against these allocations. No ledger reset or extra $10 allowance on upgrade.
- Before every completion request, verify the credential has a non-resetting OpenRouter key cap of **at most $10**. An unlimited or periodically resetting key blocks new model calls, but local position protection continues. Set this key limit in OpenRouter; the code does not modify it.
- Verify model availability/current pricing. Pin provider price ceilings and zero per-request charge, disable fallbacks, and reject unsupported extra charges.
- Atomically reserve a conservative token-cost allowance before transmission, covering system text, framing, output, and catalog cache-write pricing. Paid calls never use a zero budget as an unlimited sentinel.
- Unknown charges, timeouts and interrupted requests keep their reservations after restarts. Known usage settles atomically, including malformed model responses. Billed cost exceeding the reservation latches an API billing halt. No automatic retry of a completion request.
- Legacy paid errors with zero recorded cost receive conservative unknown-cost reservations. They may reduce available budget. Unknown legacy models require reconciliation before more calls. `total_spent` is known billing; `committed` includes uncertainty. Do not label all committed cost as confirmed spend.

Provider metering and key caps are external dependencies. The local preflight is conservative, not a claim of exact tokenization. Use a dedicated key so unrelated calls do not share the experiment's budget. Source: [OpenRouter credit limits](https://openrouter.ai/docs/api_reference/limits) and [provider price controls](https://openrouter.ai/docs/guides/routing/provider-selection#max-price).

## Market access

The hunter caches closed timeframe candles and exchange metadata, validates candle continuity and quote freshness after fetches, and persists cooldowns for 429/418. The incumbent still has its original independent adapter: a shared IP means its requests also count. This patch cannot guarantee freedom from Binance IP limits or solve network SSL failures. It deliberately does not change the incumbent.

## Validation

Unit and integration-style tests use mocked public data and OpenRouter responses. They cover actual-time fills, stale signal rejection, funding outages and later settlement, persistent halts, 24-hour expiry, migration, concurrent budget reservations, timeout accounting, key-cap enforcement and persistent market cooldowns. No production deployment or live-provider compatibility test was performed here.
