"""Config: TOML files in one folder, one file per section, validated against hard ceilings at start.

Unknown keys are an error (typos must not silently fall back to defaults).
Secrets (Telegram token, chat id, PIN, FOMO cookie, Discord token/channel/owner) come from environment variables ONLY.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


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
    swaps_per_cycle: int = 1
    history_days: int = 180
    max_candidates: int = 400             # prescreened wallets sent to fill screening per review
    dropped_cooldown_days: float = 7.0


@dataclass
class Telegram:
    api_base: str = "https://api.telegram.org"
    edit_min_interval_s: float = 5.0      # per message
    min_send_interval_s: float = 1.1      # all API writes to the chat, globally
    poll_timeout_s: int = 25


@dataclass
class Sol:
    """Solana memecoin copy-trading (wallets from the FOMO leaderboard). PAPER ONLY, its own $ book."""
    enabled: bool = True                  # still needs FOMO_COOKIE in the environment, else it stays off
    api_base: str = "https://prod-api.fomo.family"
    dex_url: str = "https://api.dexscreener.com"
    # -- the paper book and its risk limits (enforced in sol/risk.py)
    start_equity: float = 300.0
    risk_per_trade_pct: float = 1.0       # stop distance x size, % of the book
    stop_pct: float = 30.0                # stop distance from entry, % of price (memecoins are noisy)
    max_position_pct: float = 6.0         # one token, % of the book (memecoins can gap through a stop)
    max_positions: int = 8
    max_total_risk_pct: float = 10.0
    daily_loss_pct: float = 5.0
    weekly_loss_pct: float = 10.0
    max_orders_per_min: int = 30
    min_notional_usd: float = 5.0
    min_liquidity_usd: float = 25000.0    # entries refused below this pool liquidity (or if it is unknown)
    max_impact_pct: float = 2.0           # entry clamped so the estimated price impact stays under this
    max_entry_age_s: float = 30.0         # do not open on a leader swap older than this
    max_price_age_s: float = 10.0
    max_leader_feed_age_s: float = 60.0   # leader polling silent longer than this = in doubt, no entries
    swap_fee_pct: float = 1.0             # paper fee per swap (platform + network), % of notional
    extra_slippage_pct: float = 0.5
    exit_fallback_penalty_pct: float = 30.0   # exit fill when no price is known: last price minus this
    max_leaders: int = 5
    leader_pause_dd_pct: float = 10.0
    leader_pause_losses: int = 4
    # -- timing
    poll_leader_s: float = 4.0
    price_poll_s: float = 3.0
    # -- selection (same hysteresis as Hyperliquid)
    rescore_minutes: float = 60.0
    min_scored_to_start: int = 6
    join_rank: int = 5
    drop_rank: int = 10
    confirm_cycles: int = 2
    min_follow_hours: float = 24.0
    history_days: int = 60
    max_candidates: int = 120
    dropped_cooldown_days: float = 7.0
    # -- scoring strictness (sol/scoring.py)
    min_pnl_30d: float = 2000.0           # leaderboard pre-screen, USD
    min_trades: int = 40
    min_active_days: int = 14
    min_history_days: int = 21
    min_win_rate: float = 0.40
    min_profit_factor: float = 1.5
    min_positive_weeks: int = 3           # of the last 4
    max_best_trade_share: float = 0.20    # best trade / total pnl
    max_top3_share: float = 0.50
    max_token_share: float = 0.30
    min_median_hold_s: float = 60.0       # not a sniper / bundler
    max_open_buy_share: float = 0.30      # open cost basis / 30d buy volume ("holding a lot")
    max_open_vs_pnl: float = 1.0          # open cost basis / realized pnl
    max_drawdown: float = 0.30
    max_current_drawdown: float = 0.15
    max_recent_loss_pct: float = 10.0     # last 24h loss tolerated, % of total pnl
    min_copy_edge_pct: float = 1.0        # average copy return per trade after costs and lag


@dataclass
class Discord:
    api_base: str = "https://discord.com/api/v10"
    edit_min_interval_s: float = 5.0
    min_send_interval_s: float = 1.1
    poll_interval_s: float = 2.0
    prefix: str = "!"


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


@dataclass
class Config:
    risk: Risk = field(default_factory=Risk)
    broker: Broker = field(default_factory=Broker)
    selection: Selection = field(default_factory=Selection)
    telegram: Telegram = field(default_factory=Telegram)
    runtime: Runtime = field(default_factory=Runtime)
    sol: Sol = field(default_factory=Sol)
    discord: Discord = field(default_factory=Discord)
    # secrets (env only, never logged)
    tg_token: str = field(default="", repr=False)
    tg_chat_id: str = field(default="", repr=False)
    pin: str = field(default="", repr=False)
    fomo_cookie: str = field(default="", repr=False)
    fomo_cookie_file: str = field(default="", repr=False)    # alternative to FOMO_COOKIE; re-read on change
    discord_token: str = field(default="", repr=False)
    discord_channel: str = field(default="", repr=False)
    discord_owner: str = field(default="", repr=False)


SECTIONS = ("risk", "broker", "selection", "telegram", "runtime", "sol", "discord")

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
    ("broker", "taker_fee_pct"): (0.045, 1.0),
    ("broker", "extra_slippage_bps"): (0, 100),
    ("broker", "no_book_slippage_bps"): (5, 500),
    ("selection", "rescore_minutes"): (0.05, 24 * 60),
    ("selection", "join_rank"): (1, 8),
    ("selection", "drop_rank"): (8, 15),
    ("selection", "confirm_cycles"): (2, 10),
    ("selection", "min_follow_hours"): (0, 24 * 30),
    ("selection", "swaps_per_cycle"): (1, 1),
    ("selection", "history_days"): (60, 180),
    ("telegram", "edit_min_interval_s"): (0.05, 600),
    ("telegram", "min_send_interval_s"): (0.0, 60),
    ("runtime", "trading_timeout_s"): (0.1, 2.0),
    ("runtime", "weight_per_min"): (1, 1200),
    ("runtime", "tick_s"): (0.01, 1.0),
    ("sol", "start_equity"): (10, 1_000_000),
    ("sol", "risk_per_trade_pct"): (0.1, 2.0),
    ("sol", "stop_pct"): (5.0, 60.0),
    ("sol", "max_position_pct"): (0.5, 10.0),
    ("sol", "max_positions"): (0, 10),
    ("sol", "max_total_risk_pct"): (0, 10.0),
    ("sol", "daily_loss_pct"): (0.1, 5.0),
    ("sol", "weekly_loss_pct"): (0.1, 10.0),
    ("sol", "max_orders_per_min"): (1, 30),
    ("sol", "min_notional_usd"): (1.0, 1_000_000),
    ("sol", "min_liquidity_usd"): (10_000, 1e9),
    ("sol", "max_impact_pct"): (0.1, 5.0),
    ("sol", "max_entry_age_s"): (1, 120),
    ("sol", "max_price_age_s"): (1, 120),
    ("sol", "max_leader_feed_age_s"): (5, 600),
    ("sol", "swap_fee_pct"): (0.1, 5.0),
    ("sol", "extra_slippage_pct"): (0, 10),
    ("sol", "max_leaders"): (0, 7),
    ("sol", "leader_pause_dd_pct"): (0.1, 10.0),
    ("sol", "leader_pause_losses"): (1, 5),
    ("sol", "poll_leader_s"): (1, 60),
    ("sol", "price_poll_s"): (0.2, 60),
    ("sol", "join_rank"): (1, 8),
    ("sol", "drop_rank"): (2, 15),
    ("sol", "confirm_cycles"): (2, 10),
    ("sol", "history_days"): (30, 180),
    ("sol", "min_trades"): (30, 10_000),
    ("sol", "min_active_days"): (7, 180),
    ("sol", "min_history_days"): (14, 180),
    ("sol", "min_win_rate"): (0.35, 1.0),
    ("sol", "min_profit_factor"): (1.3, 100),
    ("sol", "min_positive_weeks"): (2, 4),
    ("sol", "max_best_trade_share"): (0.01, 0.30),
    ("sol", "max_top3_share"): (0.05, 0.60),
    ("sol", "max_token_share"): (0.05, 0.40),
    ("sol", "min_median_hold_s"): (30, 86_400),
    ("sol", "max_open_buy_share"): (0.0, 0.40),
    ("sol", "max_open_vs_pnl"): (0.0, 1.5),
    ("sol", "max_drawdown"): (0.01, 0.40),
    ("sol", "max_current_drawdown"): (0.01, 0.25),
    ("sol", "max_recent_loss_pct"): (0, 20),
    ("sol", "min_copy_edge_pct"): (0.5, 100),
    ("sol", "min_pnl_30d"): (500, 1e9),
    ("discord", "edit_min_interval_s"): (0.05, 600),
    ("discord", "min_send_interval_s"): (0.0, 60),
    ("discord", "poll_interval_s"): (0.05, 60),
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
            if (isinstance(v, bool) and not isinstance(default, bool)) or not ok:
                raise ConfigError(f"{path.name}: {k} must be {type(default).__name__}")
            setattr(obj, k, type(default)(v))
    validate(cfg)
    cfg.tg_token = env.get("TELEGRAM_BOT_TOKEN", "")
    cfg.tg_chat_id = env.get("TELEGRAM_CHAT_ID", "")
    cfg.pin = env.get("COPYBOT_PIN", "")
    cfg.fomo_cookie = env.get("FOMO_COOKIE", "")
    cfg.fomo_cookie_file = env.get("FOMO_COOKIE_FILE", "")
    cfg.discord_token = env.get("DISCORD_BOT_TOKEN", "")
    cfg.discord_channel = env.get("DISCORD_CHANNEL_ID", "")
    cfg.discord_owner = env.get("DISCORD_OWNER_ID", "")
    return cfg


def validate(cfg: Config) -> None:
    for (sec, key), (lo, hi) in CEILINGS.items():
        v = getattr(getattr(cfg, sec), key)
        if not (lo <= v <= hi):
            raise ConfigError(f"{sec}.{key}={v} outside allowed range [{lo}, {hi}]")
    if cfg.selection.drop_rank <= cfg.selection.join_rank:
        raise ConfigError("selection.drop_rank must be > join_rank")
    if cfg.risk.stop_pct / 100 * cfg.risk.liq_buffer_mult >= 0.9:
        raise ConfigError("risk.stop_pct x liq_buffer_mult leaves no room before liquidation")
    if cfg.sol.drop_rank <= cfg.sol.join_rank:
        raise ConfigError("sol.drop_rank must be > join_rank")


def public_dict(cfg: Config) -> dict:
    """Config without secrets, safe to log."""
    d = asdict(cfg)
    for k in ("tg_token", "tg_chat_id", "pin", "fomo_cookie", "fomo_cookie_file", "discord_token", "discord_channel", "discord_owner"):
        d.pop(k)
    return d
