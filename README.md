# FirstBot

## Command Cheat Sheet

Use this Python path from PowerShell:

```powershell
$PY="C:\Users\benne\OneDrive\Documents\FirstBot\.venv311\Scripts\python.exe"
```

Manual two-URL sports arb bot:

```powershell
& $PY -m firstbot run-manual-sports-arb --scan --polymarket-url "POLYMARKET_URL_HERE" --kalshi-url "KALSHI_URL_HERE" --seconds 30
& $PY -m firstbot run-manual-sports-arb --paper --polymarket-url "POLYMARKET_URL_HERE" --kalshi-url "KALSHI_URL_HERE" --safe-to-trade --seconds 30
& $PY -m firstbot run-manual-sports-arb --execute --polymarket-url "POLYMARKET_URL_HERE" --kalshi-url "KALSHI_URL_HERE" --safe-to-trade --seconds 30
```

PredictionHunt signal bot:

```powershell
& $PY -m firstbot run-signal-bot --paper
& $PY -m firstbot run-signal-bot --paper --once
& $PY -m firstbot run-signal-bot --execute
& $PY -m firstbot update-signal-results
```

Hot PredictionHunt arbitrage watcher:

```powershell
& $PY -m firstbot run-hot-arb --limit 250 --predictionhunt-poll-seconds 30 --hot-window-seconds 600 --max-days-to-resolution 3 --prefer-same-day --paper
& $PY -m firstbot run-hot-arb --limit 250 --predictionhunt-poll-seconds 30 --hot-window-seconds 600 --max-days-to-resolution 3 --prefer-same-day --paper --once
& $PY -m firstbot run-hot-arb --limit 250 --max-active-watches 250 --max-days-to-resolution 3 --prefer-same-day --execute
& $PY -m firstbot run-hot-arb --limit 250 --prefer-same-day --execute --readiness-seconds 5
```

PredictionHunt diagnostic scanners:

```powershell
& $PY -m firstbot scan-predictionhunt --category sports --limit 25
& $PY -m firstbot run-predictionhunt --category sports --poll-seconds 10 --max-days-to-resolution 3 --paper
& $PY -m firstbot run-predictionhunt --category sports --poll-seconds 10 --max-days-to-resolution 3 --paper --once
```

Manual/diagnostic tools:

```powershell
& $PY -m firstbot scan --config examples/markets.example.json
& $PY -m firstbot scan-input --input "Tunisia vs Japan" --candidate "Japan"
& $PY -m firstbot scan-input --input "Tunisia vs Japan" --candidate "Japan" --rules-compatible
& $PY -m firstbot doctor
& $PY -m firstbot run-live-readiness --seconds 5
& $PY -m firstbot ws-probe --kalshi-ticker "KALSHI_TICKER_HERE" --polymarket-token "POLYMARKET_TOKEN_HERE" --seconds 30
```

Conservative Kalshi/Polymarket arbitrage bot driven by PredictionHunt.

This project is intentionally dry-run first. Cross-platform prediction-market
arbitrage is not risk-free: contracts can resolve differently, liquidity can
vanish between legs, fees can change, and one exchange can fill while the other
rejects. The bot only treats an opportunity as executable after explicit risk
gates pass.

## Secret Safety

If you pasted a live API key into chat, rotate it before running this bot.
Do not put credentials in source files. Use environment variables or a local
`.env` file that is never committed.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` with your own credentials, fee coefficients, and risk limits.

## Diagnostic Dry Scan

```powershell
python -m firstbot scan --config examples/markets.example.json
```

## Scan From A Market URL Or Name

You can hand the bot a PredictionHunt URL or plain market name:

```powershell
python -m firstbot scan-input --input "https://www.predictionhunt.com/odds/tunisia-vs-japan/19281?view=arb&buy=polymarket&sell=predictfun&candidate=Japan"
```

or:

```powershell
python -m firstbot scan-input --input "Tunisia vs Japan" --candidate "Japan"
```

The resolver uses the URL/name to search Kalshi and Polymarket, then prints the
matched contracts before checking the orderbooks. If the PredictionHunt URL
mentions another platform such as Predict.fun, the bot warns you because this
project currently trades only Kalshi and Polymarket.

Only add `--rules-compatible` after you manually confirm both matched contracts
resolve the same event in the same way:

```powershell
python -m firstbot scan-input --input "Tunisia vs Japan" --candidate "Japan" --rules-compatible
```

## Scan PredictionHunt Sports Arbitrage

Set your PredictionHunt API key in `.env`:

```env
PREDICTIONHUNT_BASE_URL=https://www.predictionhunt.com
PREDICTIONHUNT_API_KEY=pmx_your_key_here
PREDICTIONHUNT_ARBS_PATH=/api/v2/arb
PREDICTIONHUNT_EV_PATH=/api/v2/ev
```

Then scan sports opportunities for the Polymarket/Kalshi pair:

```powershell
python -m firstbot scan-predictionhunt --category sports --limit 25
```

This calls PredictionHunt `GET /api/v2/arb` with
`platforms=polymarket,kalshi`, then independently fetches the live books for
the two legs and recomputes the locked profit from current asks. PredictionHunt
is treated as a discovery feed, not as proof that a trade is still valid.

This command is diagnostic only and cannot place trades.

To run the 10-second paper-trading loop:

```powershell
python -m firstbot run-predictionhunt --category sports --poll-seconds 10 --max-days-to-resolution 3 --paper
```

For a one-poll dry run:

```powershell
python -m firstbot run-predictionhunt --category sports --poll-seconds 10 --max-days-to-resolution 3 --paper --once
```

Paper trades are logged to `logs/paper_trades.jsonl`. This command is
diagnostic only and cannot place trades.

## Run Hot WebSocket Trigger Mode

This is the only live-capable workflow. Hot mode uses PredictionHunt as a
macro discovery feed for arbitrage only. Candidates open short-lived exchange
WebSocket watches for eligible Kalshi/Polymarket opportunities:

```powershell
python -m firstbot run-hot-arb --limit 250 --predictionhunt-poll-seconds 30 --hot-window-seconds 600 --max-days-to-resolution 3 --prefer-same-day --paper
```

Hot mode only accepts PredictionHunt opportunities categorized as sports or
esports. Every other category is rejected before either venue is queried.
Same-day opportunities are prioritized, but anything resolving within the
configured three-day window can be watched.

The exact two PredictionHunt legs are authoritative: one Kalshi/Polymarket BUY
YES leg and one BUY NO leg. Hot mode may resolve a Polymarket slug to the CLOB
token for the supplied side, but it never flips a side, swaps to the other
token, or creates an alternate market pairing from local name matching.

For one macro poll and immediate exit:

```powershell
python -m firstbot run-hot-arb --limit 250 --predictionhunt-poll-seconds 30 --hot-window-seconds 600 --max-days-to-resolution 3 --prefer-same-day --paper --once
```

Hot paper triggers are logged to `logs/hot_paper_trades.jsonl`, live attempts
to `logs/hot_live_trades.jsonl`, and candidate/watch lifecycle records to
`logs/hot_candidates.jsonl`. Hot mode is arbitrage-only and does not poll or
trade the PredictionHunt EV feed.

Hot mode triggers only when the live WebSocket basket has positive net profit
after calculated exchange fees and any extra configured buffers. Kalshi fees
use `C * KALSHI_FEE_RATE * p * (1 - p)` with `KALSHI_FEE_RATE=0.07` by
default. Polymarket fees use `C * POLYMARKET_FEE_RATE * p * (1 - p)`, so set
`POLYMARKET_FEE_RATE` to the coefficient for the market/category you are
trading; the default is `0.05`. Kalshi total fees are rounded up to the cent;
Polymarket total fees are rounded up to the mill. `BOT_FEE_BUFFER_CENTS` is
now only an extra safety cushion, not the main fee model.

Near misses are logged separately to `logs/hot_near_misses.jsonl`. By default,
that means fresh live baskets that did not clear positive net profit but are at
or below `100c`. You can change it with:

```powershell
python -m firstbot run-hot-arb --paper --near-miss-cost-cents 100
```

Live trading requires both command and environment gates:

```powershell
.\.venv311\Scripts\python.exe -m firstbot run-hot-arb --limit 250 --max-active-watches 250 --max-days-to-resolution 3 --prefer-same-day --execute
```

and:

```env
BOT_LIVE_TRADING=true
```

All other commands are diagnostics and cannot place orders.

Live readiness checks Polymarket's geographic eligibility endpoint and refuses
to start if trading is restricted. A runtime geographic-restriction response
also stops live trading immediately. Polymarket FOK buys must confirm inside
the active order call before the Kalshi leg is submitted; if Polymarket account
state is uncertain or a confirmed Polymarket fill cannot be paired on Kalshi,
live trading halts for manual review.

Relevant hot-arb safety settings:

```env
BOT_HOT_ALLOWED_EVENT_TYPES=sports,esports
BOT_HOT_REQUIRE_CROSS_50=true
BOT_HOT_REQUIRE_SOURCE_PRICE_ALIGNMENT=true
BOT_HOT_SOURCE_PRICE_MAX_DEVIATION_CENTS=10
BOT_HOT_GEOBLOCK_CHECK=true
```

## Run PredictionHunt Signal Bot

Signal mode consumes PredictionHunt `smart_money` and `fade_finder` WebSocket
signals, but treats them only as a trigger. Each signal is normalized, written
to `logs/signal_raw.jsonl`, matched to Kalshi or Polymarket, and rechecked
against the live book for the signal side before any paper or live decision:

```powershell
python -m firstbot run-signal-bot --paper
```

The WebSocket URL is configurable because PredictionHunt signal schemas can
vary by account/API version:

```env
PREDICTIONHUNT_WS_URL=wss://www.predictionhunt.com/your-signal-stream
PREDICTIONHUNT_SIGNAL_CHANNELS=smart_money,fade_finder
```

By default, the bot only considers clear BUY YES or BUY NO signals resolving
within 72 hours, with a current signal-side ask from `55c` to `75c`, no more
than `3c` worse than the signal price, enough displayed depth, a tight spread,
positive estimated EV, and no recent/conflicting exposure. Rejections are logged to
`logs/signal_candidates.jsonl`; accepted paper decisions go to
`logs/signal_paper_trades.jsonl`, append `strategy=signal` rows to
`logs/trade_profit.csv`, and write a spreadsheet-friendly row to
`logs/signal_paper_trades.csv`.

Use `signal_paper_trades.csv` for large paper tests. It includes the channel,
market, side, entry price, signal price, chase amount, spread, depth, contracts,
stake, score, estimated probability, expected EV, wallet/trade-size buckets,
time to resolution, and blank result columns (`result_status`,
`resolved_outcome`, `exit_value_usd`, `realized_pnl_usd`, `notes`) for later
grading.

Live signal trading requires the same environment gate:

```powershell
python -m firstbot run-signal-bot --execute
```

and:

```env
BOT_LIVE_TRADING=true
```

Signal live orders use the existing single-leg limit/FOK executor path. The bot
never uses market orders and never raises the approved signal-side price after
validation.

## Derive Polymarket CLOB API Credentials

After `POLYMARKET_PRIVATE_KEY` is set in `.env`, install dependencies and run:

```powershell
python -m pip install -r requirements.txt
python scripts/derive_polymarket_keys.py
```

To write the derived values directly into `.env`:

```powershell
python scripts/derive_polymarket_keys.py --write-env
```

This fills:

```env
POLYMARKET_API_KEY=
POLYMARKET_API_SECRET=
POLYMARKET_API_PASSPHRASE=
```

It does not fill `POLYMARKET_FUNDER_ADDRESS`. For new API users, Polymarket
calls this the deposit wallet address. Existing users may use their current
proxy/safe wallet address.

## Live Trading

Live trading is blocked unless all of these are true:

- `BOT_LIVE_TRADING=true`
- The command is `run-hot-arb --execute`
- PredictionHunt Pro `/api/v2/arb` access is available
- Arb trades have fresh Kalshi and Polymarket WebSocket snapshots
- The verified arb trade has positive profit after fees
- Final REST orderbook refresh finds a profitable matched size by walking ask
  levels until the blended basket is no longer profitable or the per-leg dollar
  cap is reached
- The target exchange adapter supports immediate/FOK-style order behavior

For Kalshi, the scaffold prepares signed REST requests when the optional
`cryptography` dependency and RSA private key are configured. For Polymarket,
the official CLOB SDK adapter requires wallet, funder/deposit wallet, and L2
API credentials in `.env`.

## Sources Used

- Kalshi order creation uses `POST /trade-api/v2/portfolio/orders` with
  `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, and
  `KALSHI-ACCESS-TIMESTAMP`.
- Kalshi markets and orderbooks are public REST endpoints under
  `https://external-api.kalshi.com/trade-api/v2`.
- Polymarket public market discovery uses Gamma API events/markets, and CLOB
  orderbooks are public. Trading uses the official CLOB SDK/client because
  orders require wallet signing plus L2 authentication.
