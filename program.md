# Autoresearch: Polymarket Stale Odds Hunter

You are an autonomous research agent improving a Polymarket paper trading bot.
Your goal: **maximize total P&L over 10-minute paper trading experiments.**

## Setup (first run only)

1. **Create a branch**: `git checkout -b autoresearch/$(date +%Y%m%d-%H%M%S)` from current main.
2. **Read the codebase** — the repo is small. Read these files for full context:
   - `README.md` — project overview
   - `src/strategies/stale_odds.py` — the strategy (your main lever)
   - `src/services/paper_execution.py` — execution engine (read-only, do not modify)
   - `config/strategies.yaml` — thresholds and parameters
   - `config/risk.yaml` — risk limits
   - `config/markets.yaml` — market filters
3. **Initialize results.tsv**: Create it with just the header row (see "Logging" below).
4. **Establish baseline**: Run `python experiment.py --duration 10` without any changes. Log the result as experiment 1 with status `keep` and description `baseline`.
5. **Begin the loop.**

## The Experiment Loop

LOOP FOREVER:

1. **Read current state**: Check `src/strategies/stale_odds.py`, config files, and `results.tsv`.
2. **Form a hypothesis**: Based on past results and known issues. Write a clear one-liner.
3. **Implement**: Make a small, targeted change. One hypothesis = one change.
4. **Commit**: `git add -A && git commit -m "experiment N: <hypothesis>"`
5. **Run**: `python experiment.py --duration 10 > experiment_result.json 2>experiment_stderr.log`
6. **Read results**: `cat experiment_result.json` — the key metric is `total_pnl`.
7. **Evaluate**:
   - If `total_pnl` improved (less negative or more positive) → **KEEP**
   - If `total_pnl` worsened → **DISCARD**
   - If `fills == 0` (no trades) → **DISCARD** (the bot must trade)
   - If `bot_crashed == true` → **DISCARD**
   - If result is marginal (within $0.50 of baseline) → run 2 more trials, average, then decide
8. **Keep or discard**:
   - KEEP: Leave the commit as-is (branch advances)
   - DISCARD: `git reset --hard HEAD~1` (revert to last good state)
9. **Log**: Append results to `results.tsv` (see "Logging" below)
10. **Go to step 1**

## What You CAN Modify

- `src/strategies/stale_odds.py` — strategy logic, signal checks, thresholds, new signal types
- `config/strategies.yaml` — thresholds, parameters, feature flags
- `config/risk.yaml` — position limits, drawdown limits, risk parameters
- `config/markets.yaml` — market filters, liquidity thresholds, category filters

## What You CANNOT Modify

- `src/services/paper_execution.py` — execution engine is fixed
- `src/main.py` — bot runner is fixed
- `src/storage/` — storage layer is fixed
- `src/adapters/` — API adapters are fixed
- `experiment.py` — experiment runner is fixed
- `program.md` — these instructions are fixed

## Logging Results

Append one line per experiment to `results.tsv` (tab-separated, NOT comma-separated).

Header:
```
experiment_id	timestamp	hypothesis	total_pnl	fills	exits	fill_rate	pnl_per_fill	kept	git_hash	notes
```

Example:
```
1	2026-03-28T14:00:00	baseline	-2.11	6	0	1.000	-0.352	true	f2da3a2	depth_imbalance only
2	2026-03-28T14:15:00	raise min edge to 0.01	-0.50	3	1	0.750	-0.167	true	a1b2c3d	fewer but better trades
3	2026-03-28T14:30:00	disable depth_imbalance	0.00	0	0	0.000	0.000	false	reverted	no signals fired
```

Do NOT commit `results.tsv` — leave it untracked.

## Known Issues (starting context)

1. **Only depth_imbalance signals fire** — complement deviation is too small (markets are efficiently priced to ~0.5 cents). Momentum and spread-widening also produce nothing.

2. **Penny token leakage** — NHL Stanley Cup tokens slip through the 0.05 price filter because the YES side is 0.85+ while NO is penny-priced. The bot buys YES at 0.85+ where there's almost no edge.

3. **Claimed edge doesn't materialize** — Signals claim 1.7-2.8 cent edge but positions drift negative. Either (a) fair value estimate is wrong, (b) market moves against us, or (c) fees eat the edge.

4. **Fee drag** — Polymarket charges 2% on fills. On a $10 position at $0.85 that's ~$0.17 in fees. If the claimed edge per position is ~$0.24, net edge is nearly zero.

5. **Stale book detection may be noise** — Depth imbalances on low-prob tokens are structural (natural book shape), not mispricings.

## Improvement Ideas (prioritized backlog)

Try these in roughly this order, but use your judgment:

1. **Tighten price range** — Only trade tokens priced 0.15-0.85 (avoid near-zero and near-one)
2. **Raise min edge to cover fees** — entry_edge_threshold should be > 2% of position price
3. **Tighten depth_imbalance ratio** — 20:1 is too loose; try 50:1 or 100:1
4. **Add dynamic hold time** — Hold longer on high-edge signals, shorter on low-edge
5. **Add volume filter** — Only signal on markets with recent trade activity
6. **Try mean-reversion exit** — Close when price reverts toward entry within 60s
7. **Wider cooldown** — 60s between signals per market may be too short; try 120-180s
8. **Scale position size by confidence** — $5 on conf=0.50, $15 on conf=1.00
9. **Focus market types** — Test excluding sports entirely vs including high-volume sports only
10. **Add a spread-capture signal** — Buy at bid, sell at ask on wide-spread markets (market-making)

## Important Notes

- This is **PAPER MODE ONLY** — no real money at risk. All APIs are free.
- The bot connects to live Polymarket data but simulates all fills locally.
- Experiments run against whatever markets are currently active — results vary by time of day.
- P&L over 10 minutes is stochastic. When results are close, run multiple trials.
- Keep the experiment log honest — log failures and crashes too.
- After each experiment, briefly note what you learned in the `notes` column.

## NEVER STOP

Once the experiment loop begins, do NOT pause to ask the human if you should continue. Do NOT ask "should I keep going?" The human might be asleep or away. You are autonomous. If you run out of ideas, think harder — re-read the strategy code for new angles, try combining previous near-misses, try more radical changes. The loop runs until the human interrupts you, period.

Each experiment takes ~12 minutes (10 min trading + 2 min analysis). That's ~5/hour or ~40 overnight.
