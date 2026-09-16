# Build evidence — 2026-09-16

- 22 Python unit tests passed: strategy modes, warmup, costs, stop gaps, stop-before-profit ordering, fee-adjusted breakeven, monotonic trailing, partial exits, funding signs/deduplication, one position, daily/drawdown halts, minimum notional, time exit, SQLite recovery, historical gap rejection and next-open signal timing.
- Synthetic execution smoke test: $100 start; entry, trailing adjustment, stop exit; $100 end after assumed fees/slippage. This demonstrates breakeven accounting, NOT investment performance.
- Research CLI completed on 25,000 synthetic flat-price minute bars. Chronological selection/holdout pipeline ran with no trades and live_ready=false. This is a plumbing check only.
- Binance public API connectivity failed in this workspace. No genuine historical strategy result or forward-paper profitability claim is made.
- Binance plugin connection was confirmed by the app, but callable Binance tools were not exposed in this execution session. Its published capability is read-only public market data.
- Docker configuration supplied but not built/run here. No continuously hosted process or real-money execution has been deployed.

The original repository contained only its short starter README. All strategy rules are new, explicit research assumptions. See VALIDATION.md for work required before considering real funds.
