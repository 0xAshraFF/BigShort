# BigShort

**Paper-only Binance USDT perpetual short-strategy research agent.** Scans newer contracts, evaluates closed 4h / 15m / 1m candles, sizes positions, simulates automatic exits, and journals decisions to SQLite. No API keys, signed requests, real orders, or withdrawals exist in this version.

Status: executable research implementation, **not a validated profitable strategy or production live trader**. It cannot guarantee preservation of capital. Do not fund an account for this release.

## Run

Python 3.11+ on Linux/macOS; no runtime dependencies.

```bash
python -m unittest discover -s tests -v
# Synthetic execution check, NOT market data; use a new database
python -m bigshort.cli --db data/demo.sqlite demo
# Real public Binance data, simulated money; no API key
python -m bigshort.cli paper --once
python -m bigshort.cli paper
```

Continuous operation requires an always-on machine with access to Binance futures public APIs. Closing ChatGPT does not host this process. Failed cycles pause new entries; missing prices are never invented.

```bash
docker compose up -d --build
docker compose logs -f --tail 50
# Persistently halt entries and simulate flattening when fresh data is available
docker compose exec paper touch /data/STOP
docker compose down
```

The kill state stays latched in SQLite after deleting STOP. Inspect the journal and start a separate experiment database to resume. Never delete history to hide losses. Docker storage persists across restarts; `down -v` deletes it.

For a local process: `touch data/STOP`. Inspect a stopped process's journal with `python -m bigshort.cli report`. A process lock prevents concurrent commands from mutating the same account; while running, use its JSON logs.

## Initial experiment

| Rule | Default |
|---|---|
| Simulated starting equity | $100 |
| Maximum planned trade loss | 1% equity, including assumed costs |
| Leverage | 2×; config limited to 1–3× |
| Maximum allocated margin | Lesser of $10 and 10% cash |
| Open positions | One short |
| Daily equity loss limit | 3% from first observed UTC-day equity |
| Peak drawdown halt | 10%, permanently latched |
| Maximum holding time | Four hours |
| Same-symbol cooldown | 15 minutes after exit |
| Assumed taker fee | 0.05% each side, not verified account rate |
| Assumed adverse slippage | 0.10% each side |
| Universe | Active USDT perpetuals, 4–90 days since futures onboarding |
| Liquidity screen | $5m 24h quote volume; spread ≤20 bps |

Size is the smaller of the risk quantity and margin/notional cap. It can use much less than $10 margin. Exchange minimum notional and quantity steps can cause skipped setups. No martingale or averaging into losses. **Isolated margin is a requirement for any future live adapter; this simulator does not change your Binance settings.**

Your $10 ×20× position was $200 notional: a 1% adverse price move costs about $2 before fees; a 5% adverse move costs about $10. Those differ from a 5% return-on-margin loss. This release uses dollar risk sizing instead of the ambiguous original loss rule.

## Entry rules

1. Completed candles only. Bollinger upper band = 20-close mean + 2 population standard deviations; compare latest high with preceding 20-bar band.
2. Require a 4h upper-band touch.
3. **Rejection:** 15m upper-band touch, body ≤35% of range, upper wick ≥40% of range.
4. **Momentum:** bearish 15m body ≥1.5× preceding 20-candle mean range, close in bottom 20% of range. Still requires 4h extension.
5. Require 1m close below previous 1m low. Structural stop above 15m high / last three 1m highs plus 0.1%. Reject stop distances outside 0.3–5% of signal price.

These are testable interpretations of “weak candle power,” not established predictors. Newness means futures onboarding, not token launch. Four days minimum supplies warmup and excludes immediate listing spikes.

## Exits

Stops execute before favorable events within a candle. Gap-through fills occur at the worse candle open plus slippage; loss can exceed planned risk. No stop guarantees a maximum loss in a gap.

Profit ladder uses **net P&L / initial allocated margin**: 5% ROE targets net breakeven; 10% locks 5%; 15% locks 10%; continue in 5-point increments. Stops only tighten. At 100% ROE close half, then manage the remainder. The early ladder can exit before reaching the partial-profit threshold.

Fees and funding are included. Funding events are persisted and deduplicated. Actual liquidation, maintenance tiers, mark-price triggers and exchange-hosted orders are **not modeled**. Paper results can differ substantially from live execution.

## Scan diagnostics

Every cycle emits timestamped JSON logs: `scan_started`, one `symbol_scan` per evaluated symbol, and `scan_summary`. Match them by `scan_id`. The summary contains duration, exchange time, filter counts, scanned symbols, signals, entries and rejection totals. `empty_universe` means no contracts survived the filters; `completed` with zero signals means eligible symbols were evaluated without an entry setup. Errors explicitly use status `error`; existing exposure, a risk halt and the kill switch have separate statuses.

Filter counters are sequential and mutually exclusive: unsupported/inactive contract, age, then volume. Their sum plus eligible equals total exchange symbols. Symbol logs include candle counts, last-close timestamps, age and freshness for each validated timeframe, plus strategy checks. Rejection reasons identify missing 4h extension, 1m breakdown, enabled 15m setup, out-of-range stops, invalid data or quote rejection. A signal refused by execution is labeled `entry_rejected_by_risk_cooldown_or_size`; this does not claim which execution guard fired.

Diagnostics go to Docker logs, not SQLite trade events, to avoid growing the account journal on every scan. Existing state/config remain compatible and unchanged. Docker's configured log rotation applies. No signals, filters, position sizing or risk thresholds were loosened.

```bash
git pull --ff-only origin codex/paper-trading-agent
docker compose up -d --build paper
docker compose logs -f --tail 100 paper
```

Run from the existing checkout on `codex/paper-trading-agent`. Rebuilding preserves the existing named data volume; do not use `down -v`.

## Research

```bash
python -m bigshort.cli download SYMBOLUSDT --start 2026-08-01 --end 2026-09-01 --out data/SYMBOLUSDT.json
python -m bigshort.cli research data/SYMBOLUSDT.json --out data/research.json
```

Replace SYMBOLUSDT with an eligible contract. Download preserves candles/funding and source metadata. Research needs ≥20,000 contiguous minutes. It tests rejection, momentum and combined rules on the first 70%, selects by training net P&L, and evaluates the selected configuration once on the final 30%. Warmup is historical; holdout starts a fresh $100 account. Signals fill at the next open. This is a single-symbol exploratory split, **not a portfolio backtest**.

Override assumptions using `--config experiment.json --db data/experiment.sqlite` before the command. Keys match `Config` in `bigshort/core.py`, e.g. `{"mode":"rejection","leverage":1}`. Configuration cannot change within an existing database.

See [validation plan](docs/VALIDATION.md), [architecture](docs/ARCHITECTURE.md), and [build evidence](docs/BUILD_EVIDENCE.md).

## Binance MCP and AI

The available Binance ChatGPT plugin supplies **public read-only market data**, not trading. This daemon uses public REST directly so paper operation does not depend on a chat. No MCP trading integration is claimed.

An AI may review reports and propose versioned experiments; it should not move stops, raise leverage or rewrite a running account's rules. No paid AI subscription/API is needed. No subscription-income target is imposed.

## Model hunter experiment

See [Hunter v2 fixes and upgrade instructions](docs/HUNTER_FIXES.md) for persistent budgets,
24-hour expiry, fresh-quote execution and account migration. The incumbent is unchanged.

The optional `hunter` service runs three isolated OpenRouter paper accounts alongside the unchanged
deterministic incumbent. Each challenger receives the same public Binance market snapshot and has
its own cash, positions, P&L, journal, and API-cost ledger. Challengers see only a delayed peer
scorecard; they do not see pending decisions, have no trade quota, and cannot increase the fixed
risk envelope. Model output may request a short or an early exit, while local stops, sizing, circuit
breakers, leverage limits, and time exits remain authoritative.

The configured candidates are Ling 3.0 Flash Fin (free), DeepSeek V4 Flash 0731 (mid), and Claude
Opus 5 (frontier). Hunter v2 limits each paid candidate to $3, with Scout restricted to free calls.
The unused free-slot budget is not transferred. A non-resetting OpenRouter key limit of at most $10
is verified before model calls; existing spending and uncertain charges remain counted. The service reads the key from the read-only Desktop file mounted in
`compose.yaml`; it never copies the credential into the repository or logs it.

```bash
docker compose up -d --no-deps --build hunter
docker compose logs -f --tail 100 hunter
```

API reference: [Binance futures market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data). Access depends on network and regional availability; do not bypass restrictions.
