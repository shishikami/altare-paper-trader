# Altare local paper trader — first, intentionally limited release

**Not financial advice. No profitability claim. Not deployed.** This public repository
retains the old `index.html` and its Git history. The new Python code is an independent
BTC/USD + ETH/USD **local simulation**, not an OKX account bot. It cannot submit real or
exchange-demo orders: no authenticated/private exchange endpoint, API key, account import,
leverage, withdrawal, transfer or borrowing code exists. USD here is synthetic ledger
cash, NOT USDT or an exchange balance. The only external requests are public HTTPS market
data reads from Kraken; public production market DATA is not a private trading endpoint.

## Safety defaults / honest scope

- Four limits in `config/example.json` are confirmed for this NEW local USD simulation:
  initial cash $4,800, daily loss $96, planned stop risk per entry $48, and a fixed-initial
  80% BTC/ETH holding ceiling ($3,840). See [confirmed/pending rules](config/CONFIRMED_RULES.md).
  All other values remain **UNAPPROVED placeholders**. `parameters_confirmed: false`; initialization creates both
  a DB stopped flag and a persistent STOP sentinel. No account is armed by install, CI or status; restarting a stopped account keeps it stopped.
  After explicit arming, a configured service may resume that same simulation after a crash. Do not confirm parameters on behalf of the user.
- Deterministic hourly trend (SMA5/SMA20) and mean-reversion (2% below SMA20; exit at mean)
  strategies, separately accounted by strategy/symbol. No averaging down/pyramiding.
  Event strategy is explicitly disabled: no trusted event input yet. AI/research adapter
  is disabled, not secretly running or periodically thinking. Strategies cannot alter config.
- Paper market fills use timestamped last public trade ± configured adverse slippage,
  fees charged on both sides, 8-decimal rounded-down quantities, no shorts or negative cash.
  Entry-to-stop planned loss includes entry and stop-exit slippage plus both fees; reject
  above $48, record the fixed trigger on entry. This is not a guaranteed actual loss cap.
  This is a simplified model, NOT order-book or partial-liquidity simulation. Large exits
  split at the per-order cap and remain exit-only until flat. Stop-loss checks every poll.
- Hard buy checks: single order, available cash including fee, symbol exposure, total gross,
  combined BTC/ETH correlated-group exposure (conservative static group, NOT an estimated
  correlation), and per-strategy share, all relative to marked post-fill equity. Total
  marked holdings additionally cannot exceed min($3,840 fixed initial ceiling, 80% of
  current post-fill equity). Tighter pending sublimits are unchanged. Price appreciation
  blocks additions under this cap; it does not implicitly liquidate holdings.
- Asia/Shanghai day loss = day-opening equity minus current equity including unrealized
  P&L and costs. At the confirmed $96 limit, new buys latch off until the next calendar day; risk-reducing
  sells remain allowed. On rollover, prior stored marks establish opening equity so the
  first newly observed loss is not discarded. This is conservative gap attribution, NOT
  a guaranteed exact midnight NAV. Stop/limit gaps can exceed loss limits, especially in
  outages. No live stop execution or exchange-side protection is promised.
- Fresh timestamped quote and completed candles required for BOTH correlated symbols
  before any fill. No price fabrication/cache fallback. During outage/stale data **all**
  fills including exits stop; losses can continue in the simulated marked portfolio.
- Serialized SQLite journal with chained SHA256 snapshots, independent cash/quantity fill
  replay at every open/transaction; missing/corrupt DB or changed config fails closed.
  This detects accidental inconsistency, not an adversary who can rewrite all hashes.
  No automatic ledger rebuild, historical account import or reset-on-error.
- Each normal strategy/candle decision is persisted atomically with fills and rejected
  decisions; repeated polls/restarts cannot duplicate it. Stop-loss/partial exits can
  continue on the same candle until flat. One runner per data dir via advisory flock;
  DB writer serialization also protects commands. Keep a full stopped backup.
- CLI `stop` prevents every subsequent fill, including exits; it does not liquidate.
  Do not delete STOP to launch an experiment. Use reviewed config + explicit CLI start
  only after separate user approval. Missing STOP alone cannot clear DB stopped state.

## Python 3.11+ / standard library only

```
python3 -m unittest discover -s tests -v     # fully offline, temporary synthetic fixtures
python3 -m papertrader --config config/example.json --data var/paper init
python3 -m papertrader --config config/example.json --data var/paper status
python3 -m papertrader --config config/example.json --data var/paper run --once  # paused
python3 -m papertrader --config config/example.json --data var/paper market-check # PUBLIC reads only
python3 -m papertrader --config config/example.json --data var/paper stop
python3 -m papertrader --config config/example.json --data var/paper report
```

`start --ack-paper-only` arms local simulation; `run` is the foreground loop. They are
separate commands. Default example CANNOT be armed. Four confirmed limits do not confirm the remaining
parameters or authorize activation. Copy and review config OUTSIDE Git, obtain confirmation
of remaining parameters and separate activation approval before setting confirmation true
and initializing an approved new data directory.
The entire config is fingerprinted at initialization; changing it later is refused.
Existing example-initialized data is not reusable after changing parameters; preserve it,
choose a separate empty data dir for an explicitly approved new experiment. Do not reset
or delete an old ledger. Never put credentials in config. `report` is JSON with cash,
positions/cost basis, fees, realized P&L, per-strategy net/realized P&L, day baseline/latch
and last stored marked equity;
it is not a fresh quote or verified performance/backtest report.

JSON cycle/refusal events go to stdout (systemd journal in deployment). Canonical journal
payloads, decisions and fills also reside inside SQLite. Raw runtime files/logs are private,
ignored by Git, and must never be committed to this PUBLIC repository.

## Public data documentation and validation

Fixed allowlist: `GET https://api.kraken.com/0/public/OHLC?pair=XBTUSD|ETHUSD&interval=60`
and `GET .../Trades?pair=...&count=1`. Private path names and arbitrary URLs are rejected;
HTTP redirects are refused. Always discard the final uncommitted OHLC candle, enforce
continuous timestamps, and use the latest trade's source timestamp for freshness, not
just local HTTP receipt time. No login or key is needed. Data availability/terms and regional
access may change; outages fail closed rather than switching providers silently.

Official sources:
- https://docs.kraken.com/api/docs/rest-api/get-ohlc-data
- https://docs.kraken.com/api/docs/rest-api/get-recent-trades

The implementation's `market-check` validates actual current source timestamps when
explicitly invoked. Offline tests use labeled synthetic fixtures, not claimed market history.

## Deployment / remaining work

See [deploy/README.md](deploy/README.md). Non-root systemd staging + manual archive preparation
only. **No SSH session to the user's server has succeeded from this box. No deployment,
Secrets upload or service activation has occurred.** No exchange keys are needed.

Not implemented: event/news ingestion, AI provider integration, backtesting, ten-strategy
research, Monte Carlo, optimized sizing, order-book liquidity/latency model, performance
attribution/benchmarks, scheduled morning/weekly notifications, web UI, automatic upgrades,
schema migration, cryptographic remote audit anchor or monitoring/alert delivery. SQLite
full-history verification each cycle is intentionally simple and O(history); production
long-run performance, retention/rotation, backup recovery and prolonged fault/soak tests
remain necessary. No historical or simulated test profits are presented as evidence of an edge.
