"""Config: TOML files in one folder, one file per section, validated against hard ceilings at start.

Unknown keys are an error (typos must not silently fall back to defaults).
Secrets (Telegram token, chat id, PIN, Discord token/channel/owner, Helius key) come from environment variables ONLY.
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
    min_profit_factor: float = 2.0        # ... and won at least this much per 1$ lost ...
    max_drawdown: float = 0.30            # ... and never fell more than this from a peak (low swings) ...
    max_open_loss: float = 0.15           # ... and is not sitting on open losses above this share of its account
    # ... and is not in a bad stretch right now (a long good history does not excuse a bad week):
    max_loss_7d: float = 0.03             # lost no more than this share of its account in 7 days (open losses count)
    max_loss_24h: float = 0.015           # ... and no more than this in the last 24 hours
    max_loss_streak: int = 4              # ... no more than this many losing round trips in a row (latest first)
    min_recent_win_rate: float = 0.45     # ... won at least this share of its last 15 round trips
    max_dd_7d: float = 0.10               # ... and its hourly equity never fell more than this within 7 days
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
    utc_offset_hours: float = 0.0         # times on every card are shown in this time zone (Brazil: -3)


@dataclass
class Discord:
    api_base: str = "https://discord.com/api/v10"
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    edit_min_interval_s: float = 5.0      # per message
    min_send_interval_s: float = 1.1      # all API writes to the channel, globally


@dataclass
class Sol:
    """Solana memecoin copy-trading of FOMO traders, found and followed on-chain (sol/chain.py). PAPER ONLY."""
    enabled: bool = True
    # history + discovery: slow background work on the free public endpoint (no key, no credits)
    rpc_url: str = "https://api.mainnet-beta.solana.com"
    rpc_interval_s: float = 0.3           # the public endpoint allows about 4 getTransaction per second
    # live copying: Helius when HELIUS_API_KEY is set (the key is appended as ?api-key=), else the public endpoint
    live_rpc_url: str = "https://mainnet.helius-rpc.com"
    live_ws_url: str = "wss://mainnet.helius-rpc.com"
    public_ws_url: str = "wss://api.mainnet-beta.solana.com"
    live_rpc_interval_s: float = 0.11     # Helius free plan: 10 requests per second (live + history share it)
    helius_daily_credits: int = 30_000    # most Helius credits the search may spend per UTC day (then: public endpoint)
    history_parallel: int = 6             # transactions read at once during a search (Helius only)
    fomo_fee_payer: str = "AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51"   # co-signs every FOMO user swap
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
    max_leader_feed_age_s: float = 300.0  # leader polling silent longer than this = in doubt, no entries
    swap_fee_pct: float = 1.0             # paper fee per swap (platform + network), % of notional
    extra_slippage_pct: float = 0.5
    exit_fallback_penalty_pct: float = 30.0   # exit fill when no price is known: last price minus this
    max_leaders: int = 5
    leader_pause_dd_pct: float = 10.0
    leader_pause_losses: int = 4
    # -- timing
    poll_leader_s: float = 120.0          # safety poll per followed wallet; the websocket wakes it at once on a trade
    price_poll_s: float = 3.0
    # -- selection (same hysteresis as Hyperliquid)
    rescore_minutes: float = 60.0
    min_scored_to_start: int = 6
    join_rank: int = 5
    drop_rank: int = 10
    confirm_cycles: int = 2
    min_follow_hours: float = 24.0
    history_days: int = 30
    max_candidates: int = 200             # FOMO traders fully scored per review (most active by sampled volume)
    dropped_cooldown_days: float = 7.0
    discover_pages: int = 30              # per review: pages of FOMO's fee payer walked back (1000 tx, ~2 min each)
    discover_per_page: int = 20           # transactions opened per page (each reveals one trader)
    max_history_txs: int = 2000           # a wallet whose newest 2000 transactions span < min_history_days is skipped
    # -- scoring strictness (sol/scoring.py)
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
    discord: Discord = field(default_factory=Discord)
    runtime: Runtime = field(default_factory=Runtime)
    sol: Sol = field(default_factory=Sol)
    # secrets (env only, never logged)
    tg_token: str = field(default="", repr=False)
    tg_chat_id: str = field(default="", repr=False)
    pin: str = field(default="", repr=False)
    dc_token: str = field(default="", repr=False)
    dc_channel_id: str = field(default="", repr=False)
    dc_owner_id: str = field(default="", repr=False)
    helius_key: str = field(default="", repr=False)          # HELIUS_API_KEY: live Solana data (optional)


SECTIONS = ("risk", "broker", "selection", "telegram", "discord", "runtime", "sol")

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
    ("selection", "max_drawdown"): (0.05, 1.0),
    ("selection", "max_open_loss"): (0.01, 1.0),
    ("selection", "max_candidates"): (10, 5000),
    ("selection", "swaps_per_cycle"): (1, 1),
    ("selection", "history_days"): (60, 180),
    ("selection", "pool_size"): (7, 400),
    ("selection", "alt_min_coins"): (2, 1000),
    ("selection", "alt_max_coin_share"): (0.1, 1.0),
    ("telegram", "edit_min_interval_s"): (0.05, 600),
    ("telegram", "utc_offset_hours"): (-12, 14),
    ("discord", "edit_min_interval_s"): (0.05, 600),
    ("discord", "min_send_interval_s"): (0.0, 60),
    ("telegram", "min_send_interval_s"): (0.0, 60),
    ("runtime", "trading_timeout_s"): (0.1, 2.0),
    ("runtime", "weight_per_min"): (1, 1200),
    ("runtime", "tick_s"): (0.01, 1.0),
    ("selection", "max_loss_7d"): (0.0, 0.10),
    ("selection", "max_loss_24h"): (0.0, 0.05),
    ("selection", "max_loss_streak"): (1, 8),
    ("selection", "min_recent_win_rate"): (0.30, 1.0),
    ("selection", "max_dd_7d"): (0.01, 0.20),
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
    ("sol", "max_candidates"): (1, 1000),
    ("sol", "discover_pages"): (1, 500),
    ("sol", "discover_per_page"): (1, 200),
    ("sol", "max_history_txs"): (100, 10_000),
    ("sol", "leader_pause_dd_pct"): (0.1, 10.0),
    ("sol", "leader_pause_losses"): (1, 5),
    ("sol", "poll_leader_s"): (1, 300),
    ("sol", "helius_daily_credits"): (0, 200_000),
    ("sol", "history_parallel"): (1, 16),
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
            if (isinstance(v, bool) and not isinstance(default, bool)) or not ok:
                raise ConfigError(f"{path.name}: {k} must be {type(default).__name__}")
            setattr(obj, k, type(default)(v))
    validate(cfg)
    cfg.tg_token = env.get("TELEGRAM_BOT_TOKEN", "")
    cfg.tg_chat_id = env.get("TELEGRAM_CHAT_ID", "")
    cfg.pin = env.get("COPYBOT_PIN", "")
    cfg.dc_token = env.get("DISCORD_BOT_TOKEN", "")
    cfg.dc_channel_id = env.get("DISCORD_CHANNEL_ID", "")
    cfg.dc_owner_id = env.get("DISCORD_OWNER_ID", "")
    cfg.helius_key = env.get("HELIUS_API_KEY", "").strip()
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
    if cfg.sol.drop_rank <= cfg.sol.join_rank:
        raise ConfigError("sol.drop_rank must be > join_rank")


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
    for k in ("tg_token", "tg_chat_id", "pin", "dc_token", "dc_channel_id", "dc_owner_id", "helius_key"):
        d.pop(k)
    return d
