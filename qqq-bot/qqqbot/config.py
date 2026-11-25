"""Load settings from config.yaml and API keys from the environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, fields
from datetime import time
from pathlib import Path

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # optional
    load_dotenv = None


def _t(s: str) -> time:
    h, m = str(s).split(":")
    return time(int(h), int(m))


@dataclass
class Config:
    # --- position size -----------------------------------------------------
    contracts: int = 1

    # --- account / mode ----------------------------------------------------
    paper: bool = True          # Alpaca paper account (fake money)
    dry_run: bool = False       # True = never send orders, just log what it would do
    symbol: str = "QQQ"
    live_feed: str = "iex"      # "iex" (free) or "sip" (paid, full market volume)

    # --- timing (US/Eastern) -----------------------------------------------
    opening_range_end: time = time(9, 45)
    first_check: time = time(10, 0)
    last_check: time = time(14, 0)
    check_every_min: int = 10
    force_exit: time = time(15, 45)

    # --- signal parameters -------------------------------------------------
    noise_lookback_days: int = 20
    noise_multiplier: float = 1.25
    atr_days: int = 5
    entry_atr_buffer: float = 0.3

    # --- exit parameters ---------------------------------------------------
    trail_activate_atr: float = 4.0
    trail_giveback: float = 0.5
    stop_atr: float = 10.0
    band_exit: bool = False         # also exit if close crosses back through band/VWAP
    band_exit_every_min: int = 10   # ...checked on this schedule (from first_check)

    # --- option selection / orders ----------------------------------------
    strike_offset: float = 0.0      # $ in the money (0 = closest strike to price)
    min_dte: int = 1
    max_dte: int = 3
    entry_fill_timeout_sec: int = 60
    exit_fill_timeout_sec: int = 30
    retry_unfilled_entry: bool = True

    # --- files -------------------------------------------------------------
    log_file: str = "logs/trades.csv"
    state_file: str = "state.json"

    # --- secrets (from env, never from yaml) -------------------------------
    api_key: str = ""
    secret_key: str = ""


_TIME_FIELDS = {"opening_range_end", "first_check", "last_check", "force_exit"}


def parse_value(key: str, value):
    return _t(value) if key in _TIME_FIELDS else value


def apply_overrides(cfg: Config, pairs: list[str]) -> Config:
    """Apply KEY=VALUE overrides, e.g. ["band_exit=true", "max_dte=7"]."""
    known = {f.name for f in fields(Config)} - {"api_key", "secret_key"}
    for pair in pairs:
        key, _, raw = pair.partition("=")
        key = key.strip()
        if key not in known:
            raise SystemExit(f"unknown setting: {key}")
        value = raw.strip() if key in _TIME_FIELDS else yaml.safe_load(raw)
        setattr(cfg, key, parse_value(key, value))
    return cfg


def load_config(path: str | Path = "config.yaml") -> Config:
    if load_dotenv:
        load_dotenv()
    raw = {}
    p = Path(path)
    if p.exists():
        raw = yaml.safe_load(p.read_text()) or {}
    known = {f.name for f in fields(Config)} - {"api_key", "secret_key"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown config keys in {path}: {sorted(unknown)}")
    kwargs = {k: parse_value(k, v) for k, v in raw.items()}
    cfg = Config(**kwargs)
    cfg.api_key = os.environ.get("ALPACA_API_KEY", "")
    cfg.secret_key = os.environ.get("ALPACA_SECRET_KEY", "")
    if cfg.contracts < 1:
        raise ValueError("contracts must be at least 1")
    return cfg
