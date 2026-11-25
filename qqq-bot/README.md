# QQQ opening-range options bot

A Python bot that trades QQQ calls and puts on Alpaca using these rules:

1. **Opening range:** the high and low of QQQ from 9:30 to 9:45 ET.
2. **Noise band:** for each of the last 20 trading days, measure how far QQQ moved from that day's open at every minute, as a percent. Average those moves per minute and multiply by 1.25.
   - Upper band = `max(today's open, yesterday's close) × (1 + noise)`
   - Lower band = `min(today's open, yesterday's close) × (1 − noise)`
3. **ATR:** a 5-day ATR, using a simple average of true range.
4. **When it checks:** never before 10:00. From 10:00 to 14:00 it checks every 10 minutes, using the 1-minute close.
5. **Calls:** buys calls if the close is above all three of: the opening-range high + 0.3×ATR, the upper band, and VWAP.
6. **Puts:** buys puts if the close is below all three of: the opening-range low − 0.3×ATR, the lower band, and VWAP.
7. **One trade a day:** buys the strike closest to the current price with the soonest expiry 1–3 days out. The order is a limit order at the mid price.
8. **Exits:** all judged on QQQ's 1-minute closes, not the option price.
   - **(a) Trailing exit:** once QQQ has moved 4×ATR in your favour, sell if it gives back half of its best move.
   - **(b) Emergency stop:** sell if QQQ moves 10×ATR against you.
   - **(c) Time exit:** otherwise, sell at 15:45. On half days this moves to 15 minutes before the close.
9. **Settings and log:** the number of contracts is set in `config.yaml`. Every check, signal, buy and sell is written to `logs/trades.csv`.

## Setup

```bash
cd qqq-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # paste your Alpaca API keys
cp config.example.yaml config.yaml   # set `contracts`, etc.
```

Get API keys from [Alpaca](https://alpaca.markets). Start with the **paper trading** keys, which use fake money. Make sure options trading is enabled on the account.

## Run

```bash
python -m qqqbot.bot --dry-run   # signals + simulated fills at mid, no orders sent
python -m qqqbot.bot             # paper-trades (paper: true in config.yaml)
```

Start it any time before 9:45 ET and leave it running, because it loops day after day. On weekends and holidays it detects that the market is closed and sleeps. If it restarts mid-day, `state.json` lets it resume an open position. That file also stops it from trading twice on the same day.

To trade real money, set `paper: false` and use your live keys. Only do this after you have watched it on paper for a while.

## The CSV log

| event | meaning |
|---|---|
| `DAY_SETUP` | The day's opening range, ATR, call/put triggers, open and previous close |
| `CHECK` | A 10-minute check with no signal, including every value it compared |
| `SIGNAL` | A check that fired |
| `BUY` / `BUY_UNFILLED` | The option, limit price, fill price, bid and ask |
| `SELL` | Exit reason, QQQ move since entry, and the option P&L in dollars |
| `ERROR` | Anything that went wrong; the bot keeps running |

## Things to know

- **The 4×ATR and 10×ATR exits almost never trigger.** QQQ's daily ATR is several dollars, so 4×ATR is a move QQQ rarely makes in one afternoon. In practice most trades will close at 15:45. Both numbers are in `config.yaml` if you want to experiment.
- **Data:** past days use Alpaca's full SIP feed, which free accounts may use for data older than 15 minutes. Today's bars use `live_feed`. `iex` is free but only sees IEX volume, so VWAP is an approximation. A paid `sip` subscription gives the real VWAP.
- **Fills:** if the mid-price buy hasn't filled after 60 seconds, the bot cancels it and tries again at the next check. A mid-price sell gets 30 seconds, then the bot sends a market order for whatever is left, so it always exits.
- This is not financial advice. Options can go to zero quickly. Paper-trade first.

## Backtest (check it against past days)

```bash
python -m qqqbot.backtest                                   # Mar 2024 -> yesterday
python -m qqqbot.backtest --start 2025-01-01 --end 2025-10-31
python -m qqqbot.backtest --no-options                      # QQQ moves only, faster
```

The backtest replays each past day through the same strategy code the live bot uses. It uses `contracts` and every other setting from `config.yaml`. For each trade, it finds the real option contract the bot would have bought and prices it from that option's 1-minute bars. It prints a summary (win rate, total and average P&L, profit factor, max drawdown, P&L by month) and saves every trade to `logs/backtest_trades.csv`.

### Testing changes fairly

```bash
# 1. baseline, with realistic fill costs, split into two halves
python -m qqqbot.backtest -q --slippage 0.03 --split 2025-07-01

# 2. one change at a time (nothing is saved; config.yaml stays as it is)
python -m qqqbot.backtest -q --slippage 0.03 --split 2025-07-01 --set band_exit=true
python -m qqqbot.backtest -q --slippage 0.03 --split 2025-07-01 --set min_dte=5 max_dte=10
python -m qqqbot.backtest -q --slippage 0.03 --split 2025-07-01 --set strike_offset=2
```

Judge each change on the **FROM 2025-07-01** block. A change is only worth keeping if it helps in both halves. If you like one, copy it into `config.yaml`, and the live bot uses it too.

| setting | what it does |
|---|---|
| `band_exit: true` | Exits when the breakout fails: calls close if QQQ falls back below max(upper band, VWAP), puts if it rises back above min(lower band, VWAP). Checked every `band_exit_every_min` minutes. |
| `strike_offset: 2` | Buys about $2 in the money instead of at the money. It costs more per contract, but less of the price is time value that decays. |
| `min_dte` / `max_dte` | Chooses longer expiries (e.g. 5–10 days), which lose value more slowly during the day. |
| `--slippage 0.03` | Makes every option buy 3¢ worse and every sell 3¢ worse, which is $6 per round trip per contract. |

Limits:
- Alpaca's option price history only starts in February 2024.
- Historical bid/ask isn't available, so fills use the option's 1-minute close instead of the mid. Real fills will be a little worse.
- The first run downloads QQQ minute data (a minute or two) and caches it in `data/`.

## Tests

```bash
python -m pytest -q
```

The tests cover the strategy math and a full buy-to-sell cycle using a fake broker.
