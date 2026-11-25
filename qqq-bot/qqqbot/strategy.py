"""Pure strategy math. No network calls here, so it is easy to test.

All minute-bar DataFrames are expected to:
  * be indexed by tz-aware bar START timestamps in America/New_York
  * contain columns: open, high, low, close, volume (and optionally vwap)
  * contain regular-session bars only (09:30 - 15:59 starts)

"Minute index" convention: a bar that starts at 09:30 closes at 09:31, so its
minute index is 1. The bar that closes at 10:00 (starts 09:59) has index 30.
This lets the noise band for "10:00" line up with the 1-minute close at 10:00.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Optional

import pandas as pd

SESSION_OPEN = time(9, 30)


def minute_index(ts: pd.Timestamp) -> int:
    """Minutes from 09:30 to the CLOSE of the bar that starts at ts."""
    return (ts.hour * 60 + ts.minute) - (9 * 60 + 30) + 1


def regular_session(bars: pd.DataFrame) -> pd.DataFrame:
    """Keep only bars that start between 09:30 and 15:59 ET."""
    if bars.empty:
        return bars
    t = bars.index.time
    mask = (t >= time(9, 30)) & (t <= time(15, 59))
    return bars[mask]


def split_by_day(bars: pd.DataFrame) -> dict[date, pd.DataFrame]:
    return {d: g for d, g in bars.groupby(bars.index.date)}


# --------------------------------------------------------------------------- #
# 1. Opening range
# --------------------------------------------------------------------------- #
def opening_range(today: pd.DataFrame, end: time = time(9, 45)) -> tuple[float, float]:
    """High and low of bars that start at/after 09:30 and before `end`."""
    t = today.index.time
    window = today[(t >= SESSION_OPEN) & (t < end)]
    if window.empty:
        raise ValueError("no bars inside the opening range window")
    return float(window["high"].max()), float(window["low"].min())


# --------------------------------------------------------------------------- #
# 2. Noise band
# --------------------------------------------------------------------------- #
def noise_sigma(history: pd.DataFrame, lookback_days: int = 20) -> pd.Series:
    """Average absolute % move from the day's open, per minute of the day.

    Uses the last `lookback_days` complete days in `history`.
    Returns a Series indexed by minute index (1..390).
    """
    days = split_by_day(history)
    use = sorted(days)[-lookback_days:]
    if len(use) < lookback_days:
        raise ValueError(f"need {lookback_days} days of history, got {len(use)}")
    moves = []
    for d in use:
        g = days[d]
        day_open = float(g["open"].iloc[0])
        m = (g["close"] / day_open - 1.0).abs()
        m.index = [minute_index(ts) for ts in g.index]
        moves.append(m)
    return pd.concat(moves, axis=1).mean(axis=1).sort_index()


def band_at(
    sigma: pd.Series,
    idx: int,
    today_open: float,
    prev_close: float,
    multiplier: float = 1.25,
) -> tuple[float, float]:
    """Upper/lower noise band at minute index `idx`.

    Gap handling: upper band anchors on max(open, prev close), lower band on
    min(open, prev close). When there is no gap both are just the open.
    """
    if idx in sigma.index:
        s = float(sigma.loc[idx])
    else:  # e.g. today is a normal day but history ended early; use nearest
        s = float(sigma.iloc[(abs(sigma.index - idx)).argmin()])
    move = multiplier * s
    upper = max(today_open, prev_close) * (1 + move)
    lower = min(today_open, prev_close) * (1 - move)
    return upper, lower


# --------------------------------------------------------------------------- #
# 3. ATR
# --------------------------------------------------------------------------- #
def daily_ohlc(history: pd.DataFrame) -> pd.DataFrame:
    """Regular-session daily OHLC built from minute bars."""
    rows = []
    for d, g in sorted(split_by_day(history).items()):
        rows.append(
            {
                "date": d,
                "open": float(g["open"].iloc[0]),
                "high": float(g["high"].max()),
                "low": float(g["low"].min()),
                "close": float(g["close"].iloc[-1]),
            }
        )
    return pd.DataFrame(rows).set_index("date")


def atr(daily: pd.DataFrame, days: int = 5) -> float:
    """Simple average of the last `days` true ranges."""
    if len(daily) < days + 1:
        raise ValueError(f"need {days + 1} daily bars for ATR({days}), got {len(daily)}")
    prev_close = daily["close"].shift(1)
    tr = pd.concat(
        [
            daily["high"] - daily["low"],
            (daily["high"] - prev_close).abs(),
            (daily["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return float(tr.iloc[-days:].mean())


# --------------------------------------------------------------------------- #
# VWAP
# --------------------------------------------------------------------------- #
def session_vwap(today: pd.DataFrame) -> float:
    vol = today["volume"].astype(float)
    if "vwap" in today.columns and today["vwap"].notna().all():
        px = today["vwap"].astype(float)
    else:
        px = (today["high"] + today["low"] + today["close"]) / 3.0
    total = vol.sum()
    if total <= 0:
        return float(today["close"].iloc[-1])
    return float((px * vol).sum() / total)


# --------------------------------------------------------------------------- #
# Day setup + entry signal
# --------------------------------------------------------------------------- #
@dataclass
class DaySetup:
    day: date
    today_open: float
    prev_close: float
    or_high: float
    or_low: float
    atr: float
    sigma: pd.Series = field(repr=False)


def build_day_setup(
    history: pd.DataFrame,
    today: pd.DataFrame,
    *,
    lookback_days: int = 20,
    atr_days: int = 5,
    or_end: time = time(9, 45),
) -> DaySetup:
    """Everything that is fixed for the day once the opening range is done.

    `history` must contain only days BEFORE today.
    """
    history = regular_session(history)
    today = regular_session(today)
    daily = daily_ohlc(history)
    or_high, or_low = opening_range(today, or_end)
    return DaySetup(
        day=today.index[0].date(),
        today_open=float(today["open"].iloc[0]),
        prev_close=float(daily["close"].iloc[-1]),
        or_high=or_high,
        or_low=or_low,
        atr=atr(daily, atr_days),
        sigma=noise_sigma(history, lookback_days),
    )


@dataclass
class SignalCheck:
    time: pd.Timestamp  # bar close time
    close: float
    upper: float
    lower: float
    vwap: float
    call_trigger: float
    put_trigger: float
    signal: Optional[str]  # "call", "put" or None


def check_signal(
    setup: DaySetup,
    today: pd.DataFrame,
    *,
    noise_multiplier: float = 1.25,
    atr_buffer: float = 0.3,
) -> SignalCheck:
    """Evaluate rules 5 & 6 on the LAST bar in `today`."""
    today = regular_session(today)
    last_ts = today.index[-1]
    close = float(today["close"].iloc[-1])
    idx = minute_index(last_ts)
    upper, lower = band_at(setup.sigma, idx, setup.today_open, setup.prev_close, noise_multiplier)
    vwap = session_vwap(today)
    call_trigger = setup.or_high + atr_buffer * setup.atr
    put_trigger = setup.or_low - atr_buffer * setup.atr

    signal = None
    if close > call_trigger and close > upper and close > vwap:
        signal = "call"
    elif close < put_trigger and close < lower and close < vwap:
        signal = "put"
    return SignalCheck(
        time=last_ts + pd.Timedelta(minutes=1),
        close=close,
        upper=upper,
        lower=lower,
        vwap=vwap,
        call_trigger=call_trigger,
        put_trigger=put_trigger,
        signal=signal,
    )


# --------------------------------------------------------------------------- #
# 8. Exits (judged on QQQ, not on the option)
# --------------------------------------------------------------------------- #
@dataclass
class ExitTracker:
    direction: str  # "call" or "put"
    entry_underlying: float
    atr: float
    trail_activate_atr: float = 4.0
    trail_giveback: float = 0.5
    stop_atr: float = 10.0
    best_move: float = 0.0  # best favourable QQQ move in $, never negative

    @property
    def sign(self) -> int:
        return 1 if self.direction == "call" else -1

    def favourable_move(self, close: float) -> float:
        return (close - self.entry_underlying) * self.sign

    def update(self, close: float) -> Optional[str]:
        """Feed one QQQ 1-minute close. Returns an exit reason or None."""
        move = self.favourable_move(close)
        self.best_move = max(self.best_move, move)

        if move <= -self.stop_atr * self.atr:
            return "emergency_stop"
        if (
            self.best_move >= self.trail_activate_atr * self.atr
            and move <= self.best_move * (1 - self.trail_giveback)
        ):
            return "trailing_giveback"
        return None
