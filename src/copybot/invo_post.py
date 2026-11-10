"""Our own trades as Invo "share predictions" (owner request 2026-11-10: build a reputation on Invo).

One wallet's trades (invo.post_wallet, default discord.public_wallet: what the Discord signal channels show) are posted
as PAPER calls in a paper portfolio of the bot's own Invo account (invo.post_username / invo.post_portfolio, made by the
owner in the app). No money is on Invo: a paper call is a prediction that Invo prices itself and shows as a post. Its
size is the trade's margin as a share of the wallet, in the portfolio's paper money ("sims"). A resize of the trade
resizes the call, a close sells it (Invo's accuracy and P&L only count closed calls).

Endpoints (read from the Invo web app's code, app.invoapp.com main.dart.js, 2026-11-10; all /v1_0, POST JSON, paper
only. Invo's /dex/ endpoints trade real money and are never called):
  /investments/get_investments_sims  {"portfolioId"} -> {"success", "portfolioRemainingSim", "error"}
  /investments/ticker/create         {"ticker", "portfolioId", "directionLong", "entrySim", "priceTarget", "stopLoss",
                                      "leverage", "liquidationPrice"} -> {"success", "baseIds": [...], "error"}
  /investments/ticker/modify         {"baseId", "simDifference", "simIncrease", "priceTarget", "stopLoss", ... nulls}
                                      -> {"success", "error", "postId", "remainingSim"}
  /investments/sell                  {"baseId", "customClosingPrice": null} -> {"success", "error", "closingPrice"}
Each answer's "error" is {"code", ...} when Invo refuses. The first real answers are logged (invo_post_answer) so the
shapes can be checked against these.

Runs in its own thread: the trading loop only queues jobs. The calls posted ({trade pos_id: Invo base id, size}) live
in data/invo_posts.json, so a restart can still resize and close them.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from copybot import log
from copybot.invo import InvoAuthError, InvoClient, InvoError

OPEN_MAX_AGE_S = 120.0     # an entry not posted within this (Invo down, portfolio not found) is dropped: Invo prices a
                           # call when it is made, so a late post would show a different entry than the trade's
RESOLVE_EVERY_S = 60.0     # the portfolio is looked up again this often while it is not found
CLOSE_RETRY_S = (5, 30, 120, 600)   # a refused close is retried after these waits (then every 10 min)
RESIZE_MIN = 0.05          # a size change below 5% of the call is not posted (small steps add up)


def _err(body: dict) -> str:
    e = body.get("error") if isinstance(body, dict) else None
    if isinstance(e, dict):
        return str(e.get("code") or e.get("message") or e)[:80]
    return str(e)[:80] if e else ""


class Poster:
    def __init__(self, client: InvoClient, username: str, portfolio: str, path: Path, stop: threading.Event,
                 alert=lambda text: None, clock=time.monotonic):
        self.client, self.username, self.portfolio_title = client, username.strip().lstrip("@"), portfolio.strip()
        self.path, self.stop, self.alert, self.clock = Path(path), stop, alert, clock
        self.jobs: queue.Queue = queue.Queue()
        self.later: list[tuple[float, dict]] = []      # (due, job): closes waiting for a retry
        self.portfolio_id = ""
        self.next_resolve = 0.0
        self.resolve_alerted = False
        self.logged_answers: set[str] = set()
        self.calls: dict[str, dict] = self.load()      # pos_id -> {"base": Invo base id, "frac": share of the wallet}

    # ---- the trading loop's side (non-blocking) -------------------------------------------------------
    def open(self, pos_id: str, ticker: str, long: bool, frac: float, stop: float | None, leverage: float) -> None:
        self.jobs.put({"do": "open", "pos": pos_id, "ticker": ticker, "long": long, "frac": frac, "stop": stop,
                       "lev": leverage, "at": self.clock()})

    def resize(self, pos_id: str, frac: float, stop: float | None) -> None:
        self.jobs.put({"do": "resize", "pos": pos_id, "frac": frac, "stop": stop})

    def close(self, pos_id: str) -> None:
        self.jobs.put({"do": "close", "pos": pos_id, "tries": 0})

    def sync(self, open_ids: set[str]) -> None:
        """At a start: calls whose trade closed while the bot was off are closed."""
        self.jobs.put({"do": "sync", "open": set(open_ids)})

    def start(self) -> None:
        threading.Thread(target=self.run, name="invo-post", daemon=True).start()

    # ---- state ---------------------------------------------------------------------------------------
    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("base")}
        except (OSError, ValueError, AttributeError):
            return {}

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.calls), encoding="utf-8")
        tmp.replace(self.path)

    # ---- the thread ----------------------------------------------------------------------------------
    def run(self) -> None:
        while not self.stop.is_set():
            self.step(timeout=1.0)

    def step(self, timeout: float = 0.0) -> None:
        """One job (or none within `timeout`); due retries go back in the queue."""
        now = self.clock()
        due = [j for t, j in self.later if t <= now]
        if due:
            self.later = [(t, j) for t, j in self.later if t > now]
            for j in due:
                self.jobs.put(j)
        try:
            job = self.jobs.get(timeout=timeout) if timeout > 0 else self.jobs.get_nowait()
        except queue.Empty:
            return
        try:
            self.do(job)
        except (InvoError, InvoAuthError) as e:
            log.warn("invo_post_failed", do=job["do"], err=str(e)[:80])
            self.retry(job)
        except Exception:
            log.exception("invo_post_error")
            self.retry(job)

    def retry(self, job: dict) -> None:
        if job["do"] == "close":
            n = job["tries"] = job.get("tries", 0) + 1
            self.later.append((self.clock() + CLOSE_RETRY_S[min(n, len(CLOSE_RETRY_S)) - 1], job))
        elif job["do"] == "open" and self.clock() - job["at"] < OPEN_MAX_AGE_S:
            self.later.append((self.clock() + 5, job))
        elif job["do"] == "sync":
            self.later.append((self.clock() + 30, job))

    def do(self, job: dict) -> None:
        kind = job["do"]
        if kind == "open" and self.clock() - job["at"] > OPEN_MAX_AGE_S:
            return log.warn("invo_post_skipped", pos=job["pos"], why="too late to post the entry")
        if not self.resolve():
            if kind == "open":
                return self.retry(job)
            return self.later.append((self.clock() + RESOLVE_EVERY_S, job))
        getattr(self, "do_" + kind)(job)

    def resolve(self) -> bool:
        """The bot account's portfolio named invo.post_portfolio (matched by the start of its title)."""
        if self.portfolio_id:
            return True
        if self.clock() < self.next_resolve:
            return False
        self.next_resolve = self.clock() + RESOLVE_EVERY_S
        user = self.client.user(self.username)
        ports = self.client.portfolios(user[0]) if user else []
        want = self.portfolio_title.lower()
        hit = next((p for p in ports if p.title.strip().lower() == want), None) \
            or next((p for p in ports if p.title.strip().lower().startswith(want)), None)
        if hit is None:
            why = f"no Invo user @{self.username}" if not user else f"@{self.username} has no portfolio " \
                  f"'{self.portfolio_title}'"
            log.warn("invo_post_no_portfolio", why=why)
            if not self.resolve_alerted:
                self.resolve_alerted = True
                self.alert(f"⚠️ Invo posts are waiting: {why} (config/invo.toml post_username / post_portfolio).")
            return False
        if hit.kind and hit.kind != "paper":
            log.warn("invo_post_no_portfolio", why=f"'{hit.title}' is not a paper portfolio")
            if not self.resolve_alerted:
                self.resolve_alerted = True
                self.alert(f"⚠️ Invo posts are off: '{hit.title}' is not a paper portfolio.")
            return False
        self.portfolio_id = hit.id
        log.info("invo_post_portfolio", title=hit.title.strip(), calls=len(self.calls))
        return True

    def answer(self, what: str, body: dict) -> dict:
        if what not in self.logged_answers:          # the first real answer of each kind, to check the shape
            self.logged_answers.add(what)
            log.info("invo_post_answer", what=what, keys=",".join(sorted(body)) if isinstance(body, dict) else "?")
        return body if isinstance(body, dict) else {}

    def balance(self) -> tuple[float, float]:
        """(paper money not in a call, the portfolio's whole paper value)."""
        b = self.answer("sims", self.client.post("/investments/get_investments_sims",
                                                 {"portfolioId": self.portfolio_id}))
        free = float(b.get("portfolioRemainingSim") or 0.0)
        body = self.client.post("/investments/get_investments", {"portfolioId": self.portfolio_id, "isOpen": True,
                                                                 "params": {"page": 1, "size": 50}})
        held = sum(float(t.get("lastSim") or t.get("entrySim") or 0.0) for t in body.get("investmentsTicker") or [])
        return free, free + held

    def do_open(self, job: dict) -> None:
        if job["pos"] in self.calls:
            return
        free, total = self.balance()
        sims = min(job["frac"] * total, free)
        if sims <= 0:
            return log.warn("invo_post_skipped", pos=job["pos"], why="no paper money left in the portfolio")
        body = self.answer("create", self.client.post("/investments/ticker/create", {
            "ticker": job["ticker"], "portfolioId": self.portfolio_id, "directionLong": job["long"],
            "entrySim": round(sims, 2), "priceTarget": None, "stopLoss": job["stop"],
            "leverage": job["lev"], "liquidationPrice": None}))
        ids = body.get("baseIds") or []
        if not body.get("success") or not ids:
            return log.warn("invo_post_refused", do="open", ticker=job["ticker"], err=_err(body))
        self.calls[job["pos"]] = {"base": str(ids[0]), "frac": job["frac"], "ticker": job["ticker"]}
        self.save()
        log.info("invo_post_open", ticker=job["ticker"], long=job["long"], sims=round(sims, 2),
                 pct=round(job["frac"] * 100, 2))

    def do_resize(self, job: dict) -> None:
        c = self.calls.get(job["pos"])
        if c is None:
            return
        delta = job["frac"] - c["frac"]
        if abs(delta) < c["frac"] * RESIZE_MIN:
            return
        free, total = self.balance()
        sims = abs(delta) * total
        if delta > 0:
            sims = min(sims, free)
        if sims <= 0:
            return
        body = self.answer("modify", self.client.post("/investments/ticker/modify", {
            "baseId": c["base"], "name": None, "willAutoClose": True, "simDifference": round(sims, 2),
            "simIncrease": delta > 0, "directionLong": None, "priceTarget": None, "priceTargetCurrency": None,
            "stopLoss": job["stop"], "liquidationPrice": None, "holdTimeUnit": None, "minHoldTime": None,
            "maxHoldTime": None, "notes": None}))
        if not body.get("success"):
            return log.warn("invo_post_refused", do="resize", ticker=c.get("ticker"), err=_err(body))
        c["frac"] = job["frac"]
        self.save()
        log.info("invo_post_resize", ticker=c.get("ticker"), pct=round(job["frac"] * 100, 2))

    def do_close(self, job: dict) -> None:
        c = self.calls.get(job["pos"])
        if c is None:
            return
        body = self.answer("sell", self.client.post("/investments/sell",
                                                    {"baseId": c["base"], "customClosingPrice": None}))
        if not body.get("success"):
            log.warn("invo_post_refused", do="close", ticker=c.get("ticker"), err=_err(body))
            if job.get("tries", 0) == 2:
                self.alert(f"⚠️ Invo would not close the {c.get('ticker')} prediction ({_err(body) or 'no reason'});"
                           f" still trying. You can close it in the app.")
            return self.retry(job)
        del self.calls[job["pos"]]
        self.save()
        log.info("invo_post_close", ticker=c.get("ticker"), price=body.get("closingPrice"))

    def do_sync(self, job: dict) -> None:
        for pos in [p for p in self.calls if p not in job["open"]]:
            self.jobs.put({"do": "close", "pos": pos, "tries": 0})
