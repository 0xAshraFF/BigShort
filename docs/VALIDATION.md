# Evidence before real money

Real-market validation has NOT been completed. Tests and synthetic demos prove code behavior, not profitability.

1. Collect versioned 1m candles, funding and historical exchange filters across listing cohorts, including delisted contracts. Today's surviving symbols introduce survivorship bias.
2. Pre-register strategy variants, costs and risk limits. Track every experiment; do not repeatedly tune on holdout data.
3. Run rolling chronological train/validation/test windows in different market regimes. The included 70/30 split is exploratory. Implement portfolio-wide chronological allocation before treating results as account evidence.
4. Stress spread/slippage at 2× and 5×, adverse funding, missing candles, service restarts, API failures, gap stops and liquidation tiers.
5. Forward-paper the frozen configuration for ≥30 days and enough trades to estimate uncertainty (target ≥100 closed trades; wait longer if signals are rare). Never force trades to reach a count.
6. Require positive holdout/forward expectancy after costs, acceptable drawdown, stable cohorts and no unresolved control failures. Report uncertainty, losing streaks, worst gaps and rejected setups. Positive results do not guarantee future returns.
7. Review and test a separate live adapter before any manually enabled real-money mode. This release always reports live_ready=false.

The supplied screenshot alone cannot establish timeframe, symbol, returns or an edge. No performance is inferred from it.
