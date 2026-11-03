"""Phase 2 (docs/PRODUCTION_PLAN.md, owner request 2026-11-03): health alerts, the outside check-in ping and the daily
backup of data/."""
import time
import zipfile
from types import SimpleNamespace

from copybot import config
from copybot.risk import Health
from copybot.runner import Bot


def bot(tmp_path, **over):
    cfg = config.load("config", env={})
    sent = []
    b = SimpleNamespace(cfg=cfg, data=tmp_path / "data", started=time.time() - 3600, health_problems={},
                        invo_watch=None, invo_watched=frozenset(), desk=None, sent=sent,
                        ui=SimpleNamespace(send=sent.append), alerts=[],
                        health=lambda: Health(mids_age_s=0.5))
    b.alert = lambda text, key=None, every_s=600: b.alerts.append(text)
    b.health_check = lambda: Bot.health_check(b)
    b._ping = lambda url, body: Bot._ping(url, body)
    b.backup = lambda dest, day: Bot.backup(b, dest, day)
    b.__dict__.update(over)
    return b


def test_each_problem_is_alerted_once_and_its_end_too(tmp_path):
    b = bot(tmp_path, health=lambda: Health(mids_age_s=500.0),
            invo_watch=SimpleNamespace(last_ok=time.time() - 900), invo_watched=frozenset({"nicush"}),
            desk=SimpleNamespace(last_ok=time.time() - 600))
    Bot.watchdog(b)
    Bot.watchdog(b)                                         # still wrong: no second message
    assert len(b.sent) == 3 and set(b.health_problems) == {"prices", "invo", "live"}
    assert any("prices are 500s old" in t for t in b.sent) and any("Invo has not answered for 15 min" in t
                                                                   for t in b.sent)
    b.health = lambda: Health(mids_age_s=0.5)
    b.invo_watch.last_ok = b.desk.last_ok = time.time()
    Bot.watchdog(b)
    assert not b.health_problems and sum("OK again" in t for t in b.sent) == 3


def test_a_quiet_invo_is_fine_when_nobody_is_followed(tmp_path):
    b = bot(tmp_path, invo_watch=SimpleNamespace(last_ok=0.0), invo_watched=frozenset())
    assert Bot.health_check(b) == {}


def test_the_check_in_ping_goes_to_fail_while_something_is_wrong(tmp_path, monkeypatch):
    pings = []
    monkeypatch.setattr(Bot, "_ping", staticmethod(lambda url, body: pings.append((url, body))))
    b = bot(tmp_path)
    b.cfg.healthcheck_url = "https://hc-ping.com/abc"
    Bot.watchdog(b)
    b._last_ping = 0.0
    b.health = lambda: Health(mids_age_s=500.0)
    Bot.watchdog(b)
    time.sleep(0.2)
    assert pings[0] == ("https://hc-ping.com/abc", b"")
    assert pings[1][0] == "https://hc-ping.com/abc/fail" and b"prices" in pings[1][1]


def test_the_daily_backup_zips_every_ledger_and_keeps_the_newest(tmp_path):
    data = tmp_path / "data"
    (data / "wallets" / "live").mkdir(parents=True)
    (data / "wallets" / "live" / "ledger.jsonl").write_text('{"ev": "genesis"}\n', encoding="utf-8")
    (data / "ledger.jsonl").write_text("{}\n", encoding="utf-8")
    (data / "bot.lock").write_text("", encoding="utf-8")
    (data / "archive" / "old").mkdir(parents=True)
    (data / "archive" / "old" / "x.jsonl").write_text("{}", encoding="utf-8")
    b = bot(tmp_path)
    b.cfg.runtime.backup_keep = 2
    dest = tmp_path / "backups"
    for day in ("2026-11-01", "2026-11-02", "2026-11-03"):
        out = Bot.backup(b, dest, day)
    names = zipfile.ZipFile(out).namelist()
    assert sorted(names) == ["ledger.jsonl", "wallets/live/ledger.jsonl"]          # no lock file, no archive
    assert [f.name[:23] for f in sorted(dest.glob("*.zip"))] == ["copybot-data-2026-11-02", "copybot-data-2026-11-03"]
    assert not b.alerts


def test_a_backup_runs_once_a_day_next_to_the_data_folder(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr("threading.Thread.start", lambda self: started.append(self._kwargs or self._args))
    (tmp_path / "data").mkdir()
    b = bot(tmp_path)
    Bot.backup_if_due(b)
    Bot.backup_if_due(b)
    assert len(started) == 1 and started[0][0] == tmp_path.resolve() / "backups"
