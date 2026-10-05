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
    cfg = config.load(tmp_path, env={"TELEGRAM_BOT_TOKEN": "123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                                     "TELEGRAM_CHAT_ID": "42", "COPYBOT_PIN": "9876"})
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
