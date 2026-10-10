from pathlib import Path
import logging

import pytest

from copybot import config, log


def test_repo_config_loads_and_respects_ceilings():
    cfg = config.load("config", env={})
    assert cfg.risk.max_leaders <= 7 and cfg.risk.max_positions <= 10
    assert 1 <= cfg.risk.risk_per_trade_pct <= 2


@pytest.mark.parametrize("key,val", [
    ("risk_per_trade_pct", 2.5), ("max_leverage", 20), ("daily_loss_pct", 6), ("max_positions", 11),
    ("max_symbol_risk_pct", 2.0), ("max_orders_per_min", 31), ("min_notional_usd", 5), ("liq_buffer_mult", 2),
])
def test_values_above_ceilings_refuse_to_start(tmp_path, key, val):
    (tmp_path / "risk.toml").write_text(f"{key} = {val}\n")
    with pytest.raises(config.ConfigError):
        config.load(tmp_path, env={})


def test_unknown_key_is_an_error(tmp_path):
    (tmp_path / "risk.toml").write_text("max_leverge = 5\n")
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.load(tmp_path, env={})


def test_secrets_only_from_env_and_never_in_public_dict(tmp_path):
    cfg = config.load(tmp_path, env={"DISCORD_BOT_TOKEN": "MTIzNDU2.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                                     "DISCORD_CHANNEL_ID": "42", "DISCORD_OWNER_ID": "7", "COPYBOT_PIN": "9876"})
    assert cfg.pin == "9876"
    s = repr(cfg) + str(config.public_dict(cfg))
    assert "9876" not in s and "AAAAAAAA" not in s


def test_log_redacts_secrets(tmp_path, capsys):
    log.setup(str(tmp_path))
    log.add_secret("s3cr3tPIN")
    log.info("x", url="https://api.telegram.org/bot123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef/getUpdates",
             pin="s3cr3tPIN")
    try:
        raise RuntimeError("boom s3cr3tPIN")
    except RuntimeError:
        log.exception("err")
    logging.getLogger("copybot").handlers[1].flush()
    out = capsys.readouterr().out + (tmp_path / "copybot.log").read_text(encoding="utf-8")
    assert "s3cr3tPIN" not in out and "ABCDEFGHIJ" not in out
    assert "event=x" in out and "***" in out


def test_scaled_side_wallet_config():
    from copybot import config as c
    base = c.load("config", env={})
    s = c.scaled(base, 5.0)
    assert s.risk.risk_per_trade_pct == 5.0 and s.risk.max_symbol_risk_pct == 7.5 and s.risk.max_total_risk_pct == 50
    assert s.risk.daily_loss_pct == 25 and s.risk.weekly_loss_pct == 50 and s.risk.consensus_risk_pct == 2.5
    big = c.scaled(base, 20.0)
    assert big.risk.daily_loss_pct == 100 and big.risk.weekly_loss_pct == 100
    assert big.risk.stop_pct == base.risk.stop_pct and big.risk.max_leverage == base.risk.max_leverage
    assert base.risk.risk_per_trade_pct == 1.0 and base.risk.side_wallets_risk_pct == [2.0, 5.0, 10.0, 20.0]


@pytest.mark.parametrize("bad", ["[0.0]", "[60.0]", "[2.0, 2.0]", "[1.0]", '["x"]'])
def test_side_wallet_levels_are_validated(tmp_path, bad):
    import shutil
    for f in Path("config").glob("*.toml"):
        shutil.copy(f, tmp_path / f.name)
    p = tmp_path / "risk.toml"
    p.write_text(p.read_text(encoding="utf-8").replace("side_wallets_risk_pct = [2.0, 5.0, 10.0, 20.0]",
                                                        f"side_wallets_risk_pct = {bad}"), encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(tmp_path, env={})


def test_solana_and_discord_sections_load_with_documented_ranges(tmp_path):
    import shutil
    d = tmp_path / "config"
    shutil.copytree("config", d)
    cfg = config.load(d, env={})
    assert cfg.sol.enabled and cfg.sol.stop_pct == 30.0
    (d / "sol.toml").write_text("stop_pct = 99.0\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="sol.stop_pct"):
        config.load(d, env={})
    (d / "sol.toml").write_text("not_a_key = 1\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="unknown key"):
        config.load(d, env={})
    (d / "sol.toml").write_text("join_rank = 6\ndrop_rank = 5\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="drop_rank"):
        config.load(d, env={})


def test_new_secrets_come_from_the_environment_and_never_appear_in_public_config():
    env = {"HELIUS_API_KEY": " SECRETVALUE ", "DISCORD_BOT_TOKEN": "DCTOKEN", "DISCORD_CHANNEL_ID": "1",
           "DISCORD_OWNER_ID": "2"}
    cfg = config.load("config", env=env)
    assert (cfg.helius_key, cfg.dc_token, cfg.dc_channel_id, cfg.dc_owner_id) == ("SECRETVALUE", "DCTOKEN", "1", "2")
    shown = str(config.public_dict(cfg)) + repr(cfg)
    assert "SECRETVALUE" not in shown and "DCTOKEN" not in shown


def test_strict_scoring_thresholds_cannot_be_loosened_past_their_ceilings(tmp_path):
    import shutil
    d = tmp_path / "config"
    shutil.copytree("config", d)
    for line in ("min_win_rate = 0.1", "max_drawdown = 0.9", "min_positive_weeks = 1", "max_open_buy_share = 0.9",
                 "min_profit_factor = 1.0"):
        (d / "sol.toml").write_text(line + "\n", encoding="utf-8")
        with pytest.raises(config.ConfigError):
            config.load(d, env={})
