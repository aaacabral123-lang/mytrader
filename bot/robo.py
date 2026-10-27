#!/usr/bin/env python3
"""MyTrader robot: runs the trader skills on a schedule against an Alpaca PAPER account.

Each run, during NYSE hours only:
  1. settles orders from earlier runs and updates the robot's cash ledger
  2. once a day, re-reads the investor's latest 13F (trader-copycat) into trader/targets.json
  3. syncs positions from Alpaca into trader/portfolio.json and prices into trader/prices.json
  4. proposes orders: first buy with trader-budget, after that trader-rebalance (drift band)
  5. checks every order with trader-risk and sends only the PASS ones to Alpaca

Settings live in trader/robo.json and trader/config.json. Keys come from the
environment (ALPACA_API_KEY, ALPACA_SECRET_KEY), never from files.
Paper only: the trading URL is fixed to paper-api.alpaca.markets.

    python3 bot/robo.py              # one run
    python3 bot/robo.py --dry-run    # everything except sending orders
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, ".claude", "skills")
T = os.path.join(ROOT, "trader")
PAPER_URL = "https://paper-api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"
# Tests point these at a local mock; anything else must stay paper.
TRADE_URL = os.environ.get("ROBO_TEST_TRADE_URL") or PAPER_URL
QUOTE_URL = os.environ.get("ROBO_TEST_DATA_URL") or DATA_URL

ROBO_DEFAULTS = {
    "enabled": True,
    "investor_cik": "1067983",
    "investor_name": "Berkshire Hathaway",
    "top": 5,
    "band": 5,
    "sell_untracked_on_new_13f": True,
}


class RoboError(Exception):
    pass


def path(*p):
    return os.path.join(T, *p)


def read_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


def write_json(p, doc):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2)
        fh.write("\n")


class Log:
    def __init__(self, now):
        self.now = now
        self.lines = []

    def __call__(self, msg):
        print(msg)
        self.lines.append(msg)

    def save(self):
        p = path("log", "robo-%s.md" % self.now.strftime("%Y-%m"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("\n## %s UTC\n\n" % self.now.strftime("%Y-%m-%d %H:%M"))
            for line in self.lines:
                fh.write("- %s\n" % line)


class Alpaca:
    def __init__(self, key, secret):
        if not TRADE_URL.startswith(("https://paper-api.", "http://127.0.0.1")):
            raise RoboError("refusing to trade outside the paper account: %s" % TRADE_URL)
        self.h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                  "Content-Type": "application/json", "Accept": "application/json"}

    def call(self, method, url, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self.h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            raise RoboError("Alpaca %s %s: HTTP %s %s" % (method, url.split("?")[0], e.code,
                                                         e.read().decode(errors="replace")[:300]))

    def clock(self):
        return self.call("GET", TRADE_URL + "/v2/clock")

    def account(self):
        return self.call("GET", TRADE_URL + "/v2/account")

    def positions(self):
        return self.call("GET", TRADE_URL + "/v2/positions")

    def order(self, oid):
        return self.call("GET", TRADE_URL + "/v2/orders/" + oid)

    def submit(self, body):
        return self.call("POST", TRADE_URL + "/v2/orders", body)

    def latest_prices(self, symbols):
        if not symbols:
            return {}
        url = QUOTE_URL + "/v2/stocks/trades/latest?feed=iex&symbols=" + ",".join(sorted(symbols))
        trades = (self.call("GET", url) or {}).get("trades", {})
        return {s: float(t["p"]) for s, t in trades.items() if t.get("p")}


def skill(script, *args):
    """Run one of the skill scripts from the repo root; returns (exit code, stdout)."""
    cmd = [sys.executable, os.path.join(SKILLS, script)] + list(args)
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if p.returncode and p.stderr.strip():
        print(p.stderr.strip(), file=sys.stderr)
    return p.returncode, p.stdout


def settle(api, state, log):
    """Book fills of orders sent in earlier runs into the cash ledger."""
    still = []
    for rec in state.get("pending", []):
        o = api.order(rec["id"])
        if o["status"] in ("filled", "canceled", "expired", "rejected", "done_for_day"):
            qty = float(o.get("filled_qty") or 0)
            px = float(o.get("filled_avg_price") or 0)
            amount = round(qty * px, 2)
            state["cash"] = round(state["cash"] + (amount if o["side"] == "sell" else -amount), 2)
            log("%s %s: %s, %s ações a $%.2f (caixa do robô: $%.2f)"
                % (o["side"], o["symbol"], o["status"], qty, px, state["cash"]))
        else:
            still.append(rec)
    state["pending"] = still


def refresh_targets(robo, state, today, log):
    """Re-read the 13F once a day. Returns True when the copied portfolio changed."""
    if state.get("last_13f_check") == today and os.path.exists(path("targets.json")):
        return False
    before = (read_json(path("targets.json"), {}) or {}).get("source")
    args = ["copy", str(robo["investor_cik"]), "--top", str(robo["top"]), "--out", "trader/targets.json"]
    if os.environ.get("SEC_USER_AGENT"):
        args += ["--user-agent", os.environ["SEC_USER_AGENT"]]
    code, _ = skill("trader-copycat/edgar13f.py", *args)
    if code:
        log("não consegui ler o 13F hoje; sigo com as metas anteriores")
        return False
    state["last_13f_check"] = today
    after = (read_json(path("targets.json"), {}) or {}).get("source")
    if after != before:
        log("novo 13F de %s: %s" % (robo["investor_name"], after))
        return before is not None
    return False


def sync_portfolio(api, state, targets):
    positions = api.positions()
    held = [{"ticker": p["symbol"], "shares": float(p["qty"]),
             "lots": [{"id": p["symbol"] + "-alpaca", "shares": float(p["qty"]),
                       "cost": float(p["cost_basis"])}]} for p in positions]
    write_json(path("portfolio.json"), {"cash": state["cash"], "positions": held,
                                        "source": "Alpaca paper positions + robot cash ledger"})
    prices = {p["symbol"]: float(p["current_price"]) for p in positions}
    want = {w["ticker"] for w in targets.get("weights", [])} - set(prices)
    prices.update(api.latest_prices(want))
    write_json(path("prices.json"), {"as_of": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                     "source": "alpaca", "prices": prices})
    return held


def propose(robo, held, new_13f):
    """Write trader/orders.json via budget (first buy) or rebalance. Returns the orders."""
    if os.path.exists(path("orders.json")):
        os.remove(path("orders.json"))
    if not held:
        skill("trader-budget/size.py", "--prices", "trader/prices.json", "--write")
    else:
        args = ["--band", str(robo["band"]), "--write"]
        if new_13f and robo.get("sell_untracked_on_new_13f"):
            args.append("--sell-untracked")
        skill("trader-rebalance/rebalance.py", *args)
    return (read_json(path("orders.json"), {}) or {}).get("orders", [])


def run(dry_run=False):
    now = dt.datetime.now(dt.timezone.utc)
    log = Log(now)
    robo = dict(ROBO_DEFAULTS, **(read_json(path("robo.json"), {}) or {}))
    cfg = read_json(path("config.json"), {}) or {}
    if not robo.get("enabled"):
        print("robô desligado em trader/robo.json")
        return 0
    if cfg.get("paper") is False:
        raise RoboError("trader/config.json diz paper: false; este robô só opera paper")
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RoboError("faltam ALPACA_API_KEY e ALPACA_SECRET_KEY (GitHub: Settings > Secrets > Actions)")
    api = Alpaca(key, secret)

    clock = api.clock()
    if not clock.get("is_open"):
        print("bolsa fechada; próxima abertura %s" % clock.get("next_open"))
        return 0
    today = str(clock["timestamp"])[:10]

    state = read_json(path("state", "robo.json")) or {"cash": float(cfg.get("budget", 0)), "pending": []}
    settle(api, state, log)
    if state["pending"]:
        log("%d ordem(ns) ainda abertas; espero a próxima rodada" % len(state["pending"]))
        return finish(state, log)

    new_13f = refresh_targets(robo, state, today, log)
    targets = read_json(path("targets.json"))
    if not targets:
        raise RoboError("sem trader/targets.json; o 13F não pôde ser lido")
    held = sync_portfolio(api, state, targets)
    orders = propose(robo, held, new_13f)
    if not orders:
        print("carteira dentro da meta; nada a fazer")
        return finish(state, log)

    code, out = skill("trader-risk/check.py", "--today", today, "--json")
    verdicts = json.loads(out)["results"] if out.strip() else []
    broker = read_json(path("state", "broker.json")) or {"orders": []}
    passed = []
    for v in verdicts:
        o = orders[v["index"] - 1]
        if v["verdict"] == "PASS":
            passed.append(o)
        else:
            log("BLOQUEADA pelo risco: %s %s %s (%s)" % (o["side"], o["symbol"], v["amount"],
                                                        "; ".join(v["reasons"])))
    # sells first so their cash is back before the buys
    for o in sorted(passed, key=lambda o: o["side"] != "sell"):
        body = {k: o[k] for k in ("symbol", "side", "type", "time_in_force", "qty", "notional") if k in o}
        if dry_run:
            log("[simulação] enviaria %s" % json.dumps(body))
            continue
        sent = api.submit(body)
        state["pending"].append({"id": sent["id"], "symbol": o["symbol"], "side": o["side"]})
        broker["orders"].append({"date": today, "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "placed",
                                 "symbol": o["symbol"], "side": o["side"], "id": sent["id"],
                                 "approved_by": "robo (automatic, paper)", "why": o.get("why", "")})
        log("enviada: %s %s %s (%s)" % (o["side"], o["symbol"],
                                        "$" + o["notional"] if "notional" in o else o.get("qty", "") + " ações",
                                        o.get("why", "")))
        if o["side"] == "sell":
            time.sleep(2)
    cutoff = (now - dt.timedelta(days=30)).strftime("%Y-%m-%d")
    broker["orders"] = [r for r in broker["orders"] if r.get("date", "") >= cutoff]
    if not dry_run:
        write_json(path("state", "broker.json"), broker)
        time.sleep(5)
        settle(api, state, log)
    return finish(state, log, save=not dry_run)


def finish(state, log, save=True):
    if save:
        write_json(path("state", "robo.json"), state)
    if log.lines:
        log.save()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="MyTrader paper-trading robot.")
    ap.add_argument("--dry-run", action="store_true", help="do everything except sending orders")
    a = ap.parse_args(argv)
    try:
        return run(a.dry_run)
    except RoboError as e:
        print("erro: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
