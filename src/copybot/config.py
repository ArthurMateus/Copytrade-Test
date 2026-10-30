"""Config: TOML files in one folder, one file per section, validated against hard ceilings at start.

Unknown keys are an error (typos must not silently fall back to defaults).
Secrets (PIN, Discord token/channel/owner, Invo token file) come from environment variables ONLY.
"""
from __future__ import annotations

import copy
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
    # trailing stop (owner request 2026-10-10): once the price is trail_after_pct in our favour, the stop follows it
    # trail_pct behind the best price seen, never below break-even (entry + round-trip fees). 0 = off
    trail_after_pct: float = 3.0
    trail_pct: float = 3.0
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


@dataclass
class Broker:
    taker_fee_pct: float = 0.045
    extra_slippage_bps: float = 1.0       # on top of walking the real book
    no_book_slippage_bps: float = 20.0    # exit fallback when the book cannot be fetched


@dataclass
class Discord:
    """The bot's chat (Telegram was removed 2026-10-10)."""
    api_base: str = "https://discord.com/api/v10"
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    edit_min_interval_s: float = 5.0      # per message
    min_send_interval_s: float = 1.1      # all API writes to the channel, globally
    utc_offset_hours: float = 0.0         # times on every card are shown in this time zone (Brazil: -3)


@dataclass
class Invo:
    """Copy Invo (Involio) traders' posted calls on paper (owner request 2026-10-09; copybot/invo.py). Needs
    INVO_TOKEN_FILE (a file holding the bot's own Invo account's refresh token), else it stays off."""
    margin_allocations: bool = False  # fixed wallet-% margin, independent trader contributions
    enabled: bool = True
    api_base: str = "https://api.invoapp.com/v1_0"
    # how fast a new call is seen (owner request 2026-10-10, a 26 s copy lag): every poll_s the watcher asks each
    # followed trader's portfolio list once, its requests spaced request_gap_s apart (7 traders: ~3.5 s + poll_s).
    # 2026-10-11 (owner asked for < 3 s): measured from the owner's PC, one Invo request takes ~0.29 s, ~6 req/s ran
    # clean and ~13 req/s began to fail, Invo has no push channel and its "moves" feed misses most calls; so no
    # pause between rounds and 0.25 s spacing (~3.4 req/s): a call is seen ~1 s after it is posted (worst ~2.5 s)
    poll_s: float = 0.0
    request_gap_s: float = 0.25
    max_call_age_s: float = 180.0         # a call older than this when we first see it is not copied (late entry)
    size_mult: float = 1.0                # our exposure = their size x leverage x this, of our equity (>= 10$)
    limits_pct: float = 5.0               # the wallet's limits are those of a side wallet at this risk level
    # cap per copy (owner request 2026-10-09, after a 25% x 5x STRK call became a 375$ copy of a 300$ wallet):
    # a copy never risks more than this % of our equity at our stop (2% at a 3% stop = 200$ on 300$)
    max_risk_pct: float = 2.0
    # real-money ("live") calls (owner request 2026-10-12): those traders often put < 1% of their account in a trade
    # (0.5% x 5x = 2.5% exposure = a 7.50$ copy at size_mult 1); x10 makes it ~75$ (still capped by max_risk_pct)
    live_size_mult: float = 10.0
    max_traders: int = 7
    # fixed-risk Invo wallets next to the trader-size one (owner request 2026-10-09): each copy risks this % of the
    # wallet's own 300$ at our 3% stop, whatever the trader's size
    risk_wallets_pct: list = field(default_factory=lambda: [1.0, 2.0, 5.0, 10.0, 20.0])
    # trailing stop of the Invo wallets (owner request 2026-10-11): replaces risk.trail_after_pct / trail_pct there.
    # Off by default: on 180 days of real prices the 3/3 trail cut the big winners this book needs (its usual wins
    # are small), lowering profit per copy; 0 = off, e.g. 5 / 5 = start at +5%, trail 5% behind
    trail_after_pct: float = 0.0
    trail_pct: float = 3.0
    # a followed trader is dropped when its copies in the trader-size wallet have lost this % of the wallet from their
    # best (audit 2026-10-11: the shared rule, 10% of equity / 7 = 4.29$, dropped an 80%-win trader on ONE normal
    # stop of a 140$ copy). 4% = two stops of the largest copy (max_risk_pct 2%); 5 losses in a row still drop it
    pause_dd_pct: float = 4.0
    # stops compared (owner request 2026-10-12: "3%, 8% and stop whenever the trader stops"): every Invo size (trader
    # size, risk_wallets_pct) runs once per stop, same calls and same copy size, only the exit differs. The one equal
    # to risk.stop_pct is the existing wallets. 0 = the trader's exit: no stop of ours but a guard just before where a
    # copy that big would be liquidated (runner.invo_emergency_stop: 30% for small copies, ~11% at 20% risk)
    stops_pct: list = field(default_factory=lambda: [3.0, 8.0, 0.0])
    # the stop of the main Invo wallets (one of stops_pct): they keep the plain names, decide which calls are copied
    # and the trader-size one posts the cards. Owner request 2026-10-12: 0 = keep a copy open until the trader closes
    # it (config/invo.toml); the default here is our usual stop. Changing it: /invoflatten, then /invoreset.
    main_stop_pct: float = 3.0
    # Invo ticker -> Hyperliquid coin when the names differ (checked against Hyperliquid's list at each use)
    aliases: dict = field(default_factory=dict)


@dataclass
class Runtime:
    info_url: str = "https://api.hyperliquid.xyz/info"
    ws_url: str = "wss://api.hyperliquid.xyz/ws"
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
    discord: Discord = field(default_factory=Discord)
    runtime: Runtime = field(default_factory=Runtime)
    invo: Invo = field(default_factory=Invo)
    # secrets (env only, never logged)
    pin: str = field(default="", repr=False)
    dc_token: str = field(default="", repr=False)
    dc_channel_id: str = field(default="", repr=False)
    dc_owner_id: str = field(default="", repr=False)
    invo_token_file: str = field(default="", repr=False)     # INVO_TOKEN_FILE: the bot's Invo login (optional)


SECTIONS = ("risk", "broker", "discord", "runtime", "invo")

# (section, key) -> (min, max). The documented hard ceilings; a config outside them refuses to start.
CEILINGS: dict[tuple[str, str], tuple[float, float]] = {
    ("risk", "start_equity"): (10, 1_000_000),
    ("risk", "risk_per_trade_pct"): (1.0, 2.0),
    ("risk", "stop_pct"): (0.2, 20.0),
    ("risk", "daily_loss_pct"): (0.1, 5.0),
    ("risk", "weekly_loss_pct"): (0.1, 10.0),
    ("risk", "max_leverage"): (1, 10),
    ("risk", "trail_after_pct"): (0.0, 50.0),
    ("risk", "trail_pct"): (0.2, 20.0),
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
    ("discord", "utc_offset_hours"): (-12, 14),
    ("discord", "edit_min_interval_s"): (0.05, 600),
    ("discord", "min_send_interval_s"): (0.0, 60),
    ("runtime", "trading_timeout_s"): (0.1, 2.0),
    ("runtime", "weight_per_min"): (1, 1200),
    ("runtime", "tick_s"): (0.01, 1.0),
    ("invo", "poll_s"): (0, 600),
    ("invo", "request_gap_s"): (0.2, 5.0),
    ("invo", "max_call_age_s"): (10, 1800),
    ("invo", "size_mult"): (0.1, 10.0),
    ("invo", "live_size_mult"): (0.1, 50.0),
    ("invo", "limits_pct"): (0.5, 20.0),
    ("invo", "max_risk_pct"): (0.1, 20.0),
    ("invo", "trail_after_pct"): (0.0, 50.0),
    ("invo", "trail_pct"): (0.2, 20.0),
    ("invo", "pause_dd_pct"): (0.5, 50.0),
    ("invo", "max_traders"): (0, 20),
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
    cfg.pin = env.get("COPYBOT_PIN", "")
    cfg.dc_token = env.get("DISCORD_BOT_TOKEN", "")
    cfg.dc_channel_id = env.get("DISCORD_CHANNEL_ID", "")
    cfg.dc_owner_id = env.get("DISCORD_OWNER_ID", "")
    cfg.invo_token_file = env.get("INVO_TOKEN_FILE", "").strip()
    return cfg


def validate(cfg: Config) -> None:
    for (sec, key), (lo, hi) in CEILINGS.items():
        v = getattr(getattr(cfg, sec), key)
        if not (lo <= v <= hi):
            raise ConfigError(f"{sec}.{key}={v} outside allowed range [{lo}, {hi}]")
    if cfg.risk.stop_pct / 100 * cfg.risk.liq_buffer_mult >= 0.9:
        raise ConfigError("risk.stop_pct x liq_buffer_mult leaves no room before liquidation")
    lv = cfg.invo.risk_wallets_pct
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v <= 50 for v in lv) \
            or len(set(lv)) != len(lv) or len(lv) > 8:
        raise ConfigError("invo.risk_wallets_pct: at most 8 distinct numbers in (0, 50]")
    st = cfg.invo.stops_pct
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 25 for v in st) \
            or len(set(st)) != len(st) or len(st) > 4 or cfg.risk.stop_pct not in st:
        raise ConfigError(f"invo.stops_pct: at most 4 distinct stops in [0, 25] including risk.stop_pct "
                          f"({cfg.risk.stop_pct:g}); 0 = the trader's exit")
    if cfg.invo.main_stop_pct not in st:
        raise ConfigError(f"invo.main_stop_pct: one of invo.stops_pct ({', '.join(f'{v:g}' for v in st)})")
    if not all(isinstance(k, str) and isinstance(v, str) and v for k, v in cfg.invo.aliases.items()):
        raise ConfigError('invo.aliases: Invo ticker = "Hyperliquid coin", e.g. { MATIC = "POL" }')


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
    return c


def public_dict(cfg: Config) -> dict:
    """Config without secrets, safe to log."""
    d = asdict(cfg)
    for k in ("pin", "dc_token", "dc_channel_id", "dc_owner_id", "invo_token_file"):
        d.pop(k)
    return d
