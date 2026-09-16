# Architecture

`market.py`: public data. `core.py`: deterministic rules and risk. `runner.py`: manage positions before scanning. `store.py`: atomic SQLite snapshots/events. `research.py`: chronological replay. `cli.py`: paper/report/demo/download/research commands.

Single-process locking prevents concurrent balance mutation. Config is immutable per database. Restart recovery paginates all closed minutes since the last processed candle. Missing management candles block progression and entries; operators must inspect errors. Stops use 1m OHLC, not tick-level execution. A new paper entry may use the whole entry minute's high on the next cycle: conservative, but not precise tick replay.

Funding is fetched before management; failures pause the cycle. Within a stop-hit minute funding is applied first, an approximation because intraminute event order is unavailable. Network failure prevents contemporaneous paper exits; recovery reconstructs them. Live exposure would need exchange-hosted stops during downtime.

No credentials, signing, live CLI or order endpoints. No hosted service was deployed by this build. Docker supports persistent self-hosting.

## Future live-adapter requirements — NOT implemented

- Verify isolated margin and position mode; never silently switch a funded account's mode.
- Trade-only, IP-restricted credentials, no withdrawal permission, outside git.
- Actual fees, lot/tick/min/max filters, leverage tiers, maintenance margin and liquidation buffers.
- Idempotent client IDs; reconcile ambiguous order timeouts before retrying.
- Confirm fills; immediately establish exchange-hosted reduce-only stops using currently supported conditional-order endpoints. Flatten/halt if protection fails.
- Stop replacements without an unprotected window; partial-fill and hedge-mode handling.
- Reconcile REST account snapshots and authenticated streams at startup/reconnect.
- Clock sync, rate limits, durable alerts, account kill switch and monitoring.
- Testnet fault injection and independent review before staged real-money activation.

Do not assume a Binance-named MCP server has these capabilities. Verify its authenticated futures tools and current endpoint specifications at implementation time.
