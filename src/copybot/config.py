"""Config: TOML files in one folder, one file per section, validated against hard ceilings at start.

Unknown keys are an error (typos must not silently fall back to defaults).
Secrets (Telegram token, chat id, PIN) come from environment variables ONLY.
"""
from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from copybot.hl import is_core_perp


MAIN_COINS = ("BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "AVAX", "LINK", "LTC")


@dataclass
class Risk:
    start_equity: float = 300.0
    risk_per_trade_pct: float = 1.0       # stop distance x size, % of wallet
    stop_pct: float = 3.0                 # stop distance from entry, % of price
    daily_loss_pct: float = 5.0
    weekly_loss_pct: float = 10.0
    max_leverage: float = 10.0
    liq_buffer_mult: float = 3.0          # liquidation distance >= this x stop distance
    max_leaders: int = 7
    max_positions: int = 10
    max_total_risk_pct: float = 10.0
    max_symbol_risk_pct: float = 1.5
    max_orders_per_min: int = 30
    min_notional_usd: float = 10.0
    max_entry_age_s: float = 10.0         # refuse to OPEN on a leader fill older than this
    clock_tolerance_ms: float = 500.0     # clock uncertainty above this = in doubt -> no entries
    max_clock_age_s: float = 300.0
    max_mids_age_s: float = 10.0
    max_leader_feed_age_s: float = 90.0   # websocket silent longer than this = leader data in doubt
    leader_pause_dd_pct: float = 10.0     # copy drawdown, % of the per-leader allocation (equity / max_leaders)
    leader_pause_losses: int = 5
    consensus_risk_pct: float = 0.5       # extra risk when a 2nd followed leader opens the same side (symbol cap still applies)
    # extra paper wallets copying the same moves at these risk levels (every limit scaled, see `scaled`)
    side_wallets_risk_pct: list = field(default_factory=lambda: [2.0, 5.0, 10.0, 20.0])


@dataclass
class Broker:
    taker_fee_pct: float = 0.045
    extra_slippage_bps: float = 1.0       # on top of walking the real book
    no_book_slippage_bps: float = 20.0    # exit fallback when the book cannot be fetched


@dataclass
class Selection:
    rescore_minutes: float = 60.0
    min_scored_to_start: int = 12
    join_rank: int = 8
    drop_rank: int = 15
    confirm_cycles: int = 2
    min_follow_hours: float = 24.0
    change_cooldown_hours: float = 24.0   # new leaders join at most this often (bad ones still leave at once)
    min_win_rate: float = 0.60            # eligible only with at least this win rate ...
    min_score: float = 70.0               # ... at least this score (0-100) ...
    min_profit_factor: float = 2.0        # ... and won at least this much per 1$ lost
    swaps_per_cycle: int = 1
    history_days: int = 180
    max_candidates: int = 2000            # prescreened wallets sent to fill screening per review
    pool_size: int = 100                  # a review stops screening once this many wallets are fully scored
    dropped_cooldown_days: float = 7.0
    main_coins: list = field(default_factory=lambda: list(MAIN_COINS))   # only these are scored and copied
    # ... unless the wallet is diversified: net profitable in >= alt_min_coins coins, none > alt_max_coin_share of
    # its profit. Such a wallet is scored on, and copied in, every perp it trades (memecoins included).
    alt_min_coins: int = 3
    alt_max_coin_share: float = 0.5


@dataclass
class Telegram:
    api_base: str = "https://api.telegram.org"
    edit_min_interval_s: float = 5.0      # per message
    min_send_interval_s: float = 1.1      # all API writes to the chat, globally
    poll_timeout_s: int = 25


@dataclass
class Runtime:
    info_url: str = "https://api.hyperliquid.xyz/info"
    ws_url: str = "wss://api.hyperliquid.xyz/ws"
    leaderboard_url: str = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
    data_dir: str = "data"
    log_dir: str = "logs"
    tick_s: float = 0.25
    trading_timeout_s: float = 2.0
    weight_per_min: int = 1200
    critical_reserve_weight: int = 300    # scoring/backfill can never use this part of the budget
    reconcile_s: float = 60.0
    clock_refresh_s: float = 60.0
    ws_alert_after_s: float = 60.0        # alert only when the websocket stays down this long (it reconnects in seconds)


@dataclass
class Config:
    risk: Risk = field(default_factory=Risk)
    broker: Broker = field(default_factory=Broker)
    selection: Selection = field(default_factory=Selection)
    telegram: Telegram = field(default_factory=Telegram)
    runtime: Runtime = field(default_factory=Runtime)
    # secrets (env only, never logged)
    tg_token: str = field(default="", repr=False)
    tg_chat_id: str = field(default="", repr=False)
    pin: str = field(default="", repr=False)


SECTIONS = ("risk", "broker", "selection", "telegram", "runtime")

# (section, key) -> (min, max). The documented hard ceilings; a config outside them refuses to start.
CEILINGS: dict[tuple[str, str], tuple[float, float]] = {
    ("risk", "start_equity"): (10, 1_000_000),
    ("risk", "risk_per_trade_pct"): (1.0, 2.0),
    ("risk", "stop_pct"): (0.2, 20.0),
    ("risk", "daily_loss_pct"): (0.1, 5.0),
    ("risk", "weekly_loss_pct"): (0.1, 10.0),
    ("risk", "max_leverage"): (1, 10),
    ("risk", "liq_buffer_mult"): (3.0, 100.0),
    ("risk", "max_leaders"): (0, 7),
    ("risk", "max_positions"): (0, 10),
    ("risk", "max_total_risk_pct"): (0, 10.0),
    ("risk", "max_symbol_risk_pct"): (0, 1.5),
    ("risk", "max_orders_per_min"): (1, 30),
    ("risk", "min_notional_usd"): (10.0, 1_000_000),
    ("risk", "max_entry_age_s"): (0.5, 60),
    ("risk", "clock_tolerance_ms"): (0, 500),
    ("risk", "max_clock_age_s"): (5, 3600),
    ("risk", "max_mids_age_s"): (1, 120),
    ("risk", "max_leader_feed_age_s"): (5, 600),
    ("risk", "leader_pause_dd_pct"): (0.1, 10.0),
    ("risk", "leader_pause_losses"): (1, 5),
    ("risk", "consensus_risk_pct"): (0, 1.0),
    ("broker", "taker_fee_pct"): (0.045, 1.0),
    ("broker", "extra_slippage_bps"): (0, 100),
    ("broker", "no_book_slippage_bps"): (5, 500),
    ("selection", "rescore_minutes"): (0.05, 24 * 60),
    ("selection", "join_rank"): (1, 8),
    ("selection", "drop_rank"): (8, 15),
    ("selection", "confirm_cycles"): (2, 10),
    ("selection", "min_follow_hours"): (0, 24 * 30),
    ("selection", "change_cooldown_hours"): (0, 24 * 7),
    ("selection", "min_win_rate"): (0.0, 1.0),
    ("selection", "min_score"): (1, 100),
    ("selection", "min_profit_factor"): (1.0, 10.0),
    ("selection", "max_candidates"): (10, 5000),
    ("selection", "swaps_per_cycle"): (1, 1),
    ("selection", "history_days"): (60, 180),
    ("selection", "pool_size"): (7, 400),
    ("selection", "alt_min_coins"): (2, 1000),
    ("selection", "alt_max_coin_share"): (0.1, 1.0),
    ("telegram", "edit_min_interval_s"): (0.05, 600),
    ("telegram", "min_send_interval_s"): (0.0, 60),
    ("runtime", "trading_timeout_s"): (0.1, 2.0),
    ("runtime", "weight_per_min"): (1, 1200),
    ("runtime", "tick_s"): (0.01, 1.0),
    ("runtime", "ws_alert_after_s"): (1, 3600),
}


class ConfigError(ValueError):
    pass


def load(config_dir: str | os.PathLike, env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config()
    cdir = Path(config_dir)
    for sec in SECTIONS:
        path = cdir / f"{sec}.toml"
        if not path.exists():
            continue
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        obj = getattr(cfg, sec)
        known = {f.name for f in fields(obj)}
        for k, v in data.items():
            if k not in known:
                raise ConfigError(f"{path.name}: unknown key {k!r}")
            default = getattr(obj, k)
            ok = isinstance(v, type(default)) or (isinstance(default, float) and isinstance(v, int))
            if isinstance(v, bool) or not ok:
                raise ConfigError(f"{path.name}: {k} must be {type(default).__name__}")
            setattr(obj, k, type(default)(v))
    validate(cfg)
    cfg.tg_token = env.get("TELEGRAM_BOT_TOKEN", "")
    cfg.tg_chat_id = env.get("TELEGRAM_CHAT_ID", "")
    cfg.pin = env.get("COPYBOT_PIN", "")
    return cfg


def validate(cfg: Config) -> None:
    for (sec, key), (lo, hi) in CEILINGS.items():
        v = getattr(getattr(cfg, sec), key)
        if not (lo <= v <= hi):
            raise ConfigError(f"{sec}.{key}={v} outside allowed range [{lo}, {hi}]")
    if cfg.selection.drop_rank <= cfg.selection.join_rank:
        raise ConfigError("selection.drop_rank must be > join_rank")
    sides = cfg.risk.side_wallets_risk_pct
    if not all(isinstance(x, (int, float)) and not isinstance(x, bool) and 0 < x <= 50 for x in sides):
        raise ConfigError("risk.side_wallets_risk_pct must be numbers in (0, 50]")
    if len(set(sides)) != len(sides) or cfg.risk.risk_per_trade_pct in sides or len(sides) > 8:
        raise ConfigError("risk.side_wallets_risk_pct: at most 8 distinct levels, none equal to risk_per_trade_pct")
    coins = cfg.selection.main_coins
    if not coins or not all(isinstance(c, str) and is_core_perp(c) for c in coins):
        raise ConfigError("selection.main_coins must be a non-empty list of perp names like \"BTC\"")
    if cfg.risk.stop_pct / 100 * cfg.risk.liq_buffer_mult >= 0.9:
        raise ConfigError("risk.stop_pct x liq_buffer_mult leaves no room before liquidation")


def scaled(cfg: Config, risk_pct: float) -> Config:
    """A copy of `cfg` for a side wallet at `risk_pct` per trade: every risk limit scales by the same factor
    (owner decision 2026-10-05: per-symbol, total and consensus risk, and the daily/weekly loss stops, capped
    at 100%). Leverage, the stop distance and the liquidation buffer do not change. Side wallets are allowed
    above the main wallet's CEILINGS on purpose; they are paper comparisons."""
    c = copy.deepcopy(cfg)
    f = risk_pct / cfg.risk.risk_per_trade_pct
    r = c.risk
    r.risk_per_trade_pct = risk_pct
    r.max_symbol_risk_pct *= f
    r.max_total_risk_pct *= f
    r.consensus_risk_pct *= f
    r.daily_loss_pct = min(100.0, r.daily_loss_pct * f)
    r.weekly_loss_pct = min(100.0, r.weekly_loss_pct * f)
    r.side_wallets_risk_pct = []
    return c


def public_dict(cfg: Config) -> dict:
    """Config without secrets, safe to log."""
    d = asdict(cfg)
    for k in ("tg_token", "tg_chat_id", "pin"):
        d.pop(k)
    return d
