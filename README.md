# poly-maker

A maker-only market-making bot for **Polymarket CLOB V2**, focused on political
markets. Single async process, local-file config, typed and
tested.

> [!WARNING]
> Market making on Polymarket is competitive and can lose money. This is a
> reference implementation and a research harness, not a guaranteed-profitable
> product. Test in `--paper` mode first; go live with small size.

## What it does

- Discovers political markets via the **Gamma API** (seconds) and ranks them by
  reward + rebate income vs. volatility/spread risk.
- Maintains a live order book per token from the **market WebSocket**.
- Quotes **maker-only** — every order is post-only. Fair-value + inventory-skew
  strategy that posts BUY-YES and BUY-NO as a two-sided quote, with live
  volatility/toxicity estimation and a regime machine that pulls quotes during
  news events (see [Strategy](#strategy)).
- Reconciles a target quote set against live orders with churn tolerances; runs
  the exchange **heartbeat** dead-man switch; enforces risk caps and a daily-loss
  kill switch.
- Config, market selection, and state are **local files + SQLite**. An operator
  with the repo, a `.env`, and a funded wallet is a complete deployment.

## Install

Uses [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```bash
uv sync --extra dev          # install deps + dev tools
uv run polymaker --help
```

## Configure

```bash
cp .env.example .env         # then edit two values:
```

- `PK` — the private key of your signer wallet
- `BROWSER_ADDRESS` — your Polymarket address (shown on the profile / developer page)

Everything else is TOML under [`config/`](config/):

- `config.toml` — wallet/engine/risk/execution settings
- `strategy.toml` — 命名参数 profile（`political-generic` = 默认兜底、
  `newsom-mm`、`romania-pm`；`livecfg/strategy.toml` 里还有 `live-tiny`）
- `markets.toml` — the trade list (populated via the CLI below)

## Use

```bash
# 1. discover + rank political markets (writes to state.db)
uv run polymaker scan                 # 默认跳过 24 小时内就要结算的市场
uv run polymaker scan --min-hours-to-end 72 --csv-limit 1000
uv run polymaker markets              # `ends` 列 = 剩余寿命

# 2. add markets to the trade list
uv run polymaker markets-add <slug> --profile political-generic

# 3. dry run: full pipeline against the live feed, no orders posted
uv run polymaker run --paper

# 4. preflight the wallet before going live
uv run polymaker doctor

# 5. self-tests: a deep post-only order (free), then a real fill round-trip (~cents)
uv run polymaker livetest      # place a deep post-only order + cancel (no fill)
uv run polymaker moneydoctor   # limit rest + market buy + market sell, auto-flattens

# 6. go live
uv run polymaker run

# ops
uv run polymaker status        # positions / open orders
uv run polymaker cancel-all    # panic button
```

## Architecture

```
market WS ─▶ OrderBook ─▶ (wake) ─▶ Quoter ─▶ strategy (pure) ─▶ reconcile ─▶ ExecutionGateway
user WS   ─▶ StateStore                                         RiskManager ┘   (post-only, heartbeat)
Gamma     ─▶ Catalog/scanner ─▶ SQLite            periodic REST reconcile ┘
```

One async event loop. The strategy layer is a pure function `(book, inventory,
params, clock) → TargetQuotes` — deterministic and unit-tested. The engine owns
all I/O and state around it; the `ExecutionGateway` wraps `py-clob-client-v2`
(which handles the V2 EIP-712 signing) and offloads its blocking calls to a
thread pool so the hot path never stalls. State (positions, orders, PnL, catalog)
lives in one SQLite file; raw WS/order events are journaled to `journal/` for
replay.

## Strategy

Maker-only, quoting both sides of each market as USDC-collateralized bids:

- **Fair value** — depth-weighted microprice off the live book, nudged by an
  EWMA of signed trade flow.
- **Quote construction** — reservation price `r = FV − skew(inventory)`;
  half-spread `δ = base + c_vol·σ + c_tox·toxicity`. Post **BUY-YES at `r − δ`**
  and **BUY-NO at `(1 − r) − δ`**. Because both legs are bids that sum below 1,
  a filled pair merges back to USDC at locked edge `1 − p − q` — a maker-only
  exit that never crosses the spread.
- **Inventory skew** — net position leans both quotes: long YES → bid YES lower,
  bid NO higher (acquire the offsetting leg). Size tapers as inventory approaches
  a soft cap, then the adding side is pulled entirely.
- **Volatility / toxicity** — realized-vol and per-fill markout (adverse
  selection) EWMAs widen the spread and shrink size in markets that pick us off.
- **Regime machine** — per market: `QUIET` (farm rewards in-band), `TRENDING`
  (lean + widen + half size), `EVENT` (sweep/jump detected → pull quotes, cool
  off), `REDUCE_ONLY` (inventory cap / near end date → exits only), `HALTED`
  (stale data / resolved / kill switch → cancel all).
- **Rewards + rebates** — quotes stay inside the liquidity-rewards band in QUIET;
  the market selector also scores the new maker-rebate program (a share of taker
  fees rebated to makers).
- **Risk** — per-market notional cap, neg-risk event-group worst-case cap, total
  exposure cap, daily-loss kill switch, WS-staleness halt.

Tune it all via profiles in `config/strategy.toml`。`political-generic` 是保守的
默认兜底（`markets-add` 不传 `--profile` 时用它）；打算上额度的市场，请给它单独
建一个 profile。

> [!IMPORTANT]
> 所有 `*_ticks` 参数都是「个数 × tick_size」，同一组数字在不同市场上代表的
> 金额完全不同：`delta_min_ticks = 2` 在 0.001 tick 的市场上是 0.2 美分，在
> 0.01 tick 的市场上却是 2 美分。复用某个 profile 前，先核对 `markets.csv`
> 里的 `tick` / `spread` / `best_bid` / `volume_24h`。

## Develop

```bash
uv run pytest                 # unit suite (offline)
POLYMAKER_LIVE=1 uv run pytest tests/test_live_marketdata.py   # live WS integration
uv run ruff check src tests   # lint
uv run mypy src               # types (strict)
```

## Status

Implemented and live-verified end to end (auth → book → strategy → sign → post →
cancel): config, catalog/scanner, order book + analytics, strategy (FV,
vol/toxicity, regime, quoting), state store + lifecycle, execution gateway +
reconciler + heartbeat, market/user websockets, risk manager, merger (EOA path),
engine, CLI, paper mode, journal capture. 83 tests; ruff + mypy strict clean.

Not yet built: a replay backtester over the captured journals, and external data
feeds (polls / news / cross-venue). Merging through a Safe/proxy wallet routes a
tx via the relayer and isn't wired yet — until then inventory exits via limit
sells rather than merging.

## License

MIT
