"""Append-only CSV log of every check, signal, buy and sell."""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

COLUMNS = [
    "timestamp_et",
    "event",          # DAY_SETUP, CHECK, SIGNAL, BUY, BUY_UNFILLED, SELL, ERROR ...
    "direction",      # call / put
    "qqq_close",
    "or_high",
    "or_low",
    "atr",
    "upper_band",
    "lower_band",
    "vwap",
    "call_trigger",
    "put_trigger",
    "option_symbol",
    "qty",
    "limit_price",
    "fill_price",
    "exit_reason",
    "qqq_move",       # favourable QQQ move since entry, $
    "pnl_dollars",    # option P&L for the trade (on SELL rows)
    "dry_run",
    "note",
]


class TradeLog:
    def __init__(self, path: str | Path, dry_run: bool = False):
        self.path = Path(path)
        self.dry_run = dry_run
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="") as f:
                csv.writer(f).writerow(COLUMNS)

    def write(self, event: str, **values) -> None:
        unknown = set(values) - set(COLUMNS)
        if unknown:
            raise KeyError(f"unknown log columns: {sorted(unknown)}")
        row = {c: "" for c in COLUMNS}
        row["timestamp_et"] = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
        row["event"] = event
        row["dry_run"] = self.dry_run
        for k, v in values.items():
            row[k] = round(v, 4) if isinstance(v, float) else v
        with self.path.open("a", newline="") as f:
            csv.DictWriter(f, fieldnames=COLUMNS).writerow(row)
