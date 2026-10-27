#!/usr/bin/env python3
"""MyTrader robot: runs the trader skills on a schedule against an Alpaca PAPER account.

Each run, during NYSE hours only:
  1. settles orders from earlier runs and updates the robot's cash ledger
  2. refreshes trader/targets.json with the chosen strategy:
       "screener" (default): every few days, trader-analyst reads the SEC numbers of the
       universe, trader-screener applies trader/rules.json, the top picks get equal weights
       "copycat": once a day, the investor's latest 13F (trader-copycat)
  3. syncs positions from Alpaca into trader/portfolio.json and prices into trader/prices.json
  4. once a day, writes trader/relatorios/<date>.md: portfolio, trader-news and trader-pelosi
  5. sells any position whose trader-alerts stop rang, and keeps it out for a cooldown
  6. otherwise proposes orders: first buy with trader-budget, after that trader-rebalance (drift band)
  7. checks every order with trader-risk and sends only the PASS ones to Alpaca

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
    "strategy": "screener",
    "universe": ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "AVGO", "TSLA", "ORCL", "CRM",
                 "ADBE", "AMD", "NFLX", "CSCO", "QCOM", "TXN", "INTC", "JPM", "BAC", "WFC",
                 "V", "MA", "AXP", "UNH", "LLY", "JNJ", "MRK", "ABBV", "PFE", "AMGN",
                 "TMO", "ISRG", "PG", "KO", "PEP", "COST", "WMT", "HD", "LOW", "MCD",
                 "NKE", "SBUX", "BKNG", "DIS", "XOM", "CVX", "CAT", "DE", "HON", "UPS"],
    "screen_every_days": 7,
    "investor_cik": "1067983",
    "investor_name": "Berkshire Hathaway",
    "top": 5,
    "band": 5,
    "sell_untracked_on_new_13f": True,
    "stop_loss_pct": 15,
    "stop_cooldown_days": 30,
    "daily_report": True,
}
STOP_PREFIX = "robo-stop-"
TXN_TYPES = {"P": "compra", "S": "venda", "S (partial)": "venda parcial", "E": "troca"}


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


def skill_json(script, *args):
    """Run a skill script with --json; None when it failed or printed no JSON."""
    _, out = skill(script, *(list(args) + ["--json"]))
    try:
        return json.loads(out)
    except ValueError:
        return None


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


def refresh_targets(api, robo, state, today, log):
    """Returns True when the stocks to hold changed (then stocks that left are sold)."""
    if robo.get("strategy", "screener") == "screener":
        return refresh_screen(api, robo, state, today, log)
    return refresh_13f(robo, state, today, log)


def refresh_screen(api, robo, state, today, log):
    """trader-analyst on the universe, then trader-screener; equal weights over the top picks.
    A stock already picked stays while it still passes the rules, so the ranking alone never churns it."""
    last = state.get("last_screen")
    every = int(robo.get("screen_every_days") or 7)
    if last and os.path.exists(path("targets.json")) and \
            (dt.date.fromisoformat(today) - dt.date.fromisoformat(last)).days < every:
        return False
    universe = [t.upper() for t in robo["universe"]]
    if os.path.exists(path("metrics.json")):
        os.remove(path("metrics.json"))
    skill("trader-analyst/facts.py", *(universe + ["--no-filings", "--metrics", "trader/metrics.json"]))
    if not os.path.exists(path("metrics.json")):
        log("não consegui ler os balanços na SEC (trader-analyst); sigo com as ações anteriores")
        return False
    write_json(path("state", "universe_prices.json"), {"as_of": today, "source": "alpaca",
                                                       "prices": api.latest_prices(universe)})
    res = skill_json("trader-screener/screen.py", "trader/metrics.json", "--rules", "trader/rules.json",
                     "--prices", "trader/state/universe_prices.json", "--out", "trader/shortlist.json")
    if res is None:
        log("o screener falhou; sigo com as ações anteriores")
        return False
    state["last_screen"] = today
    passed = [r["ticker"] for r in sorted(res.get("passed", []), key=lambda r: -(r.get("score") or 0))]
    names = {r["ticker"]: r.get("name", "") for r in res.get("passed", [])}
    prev_doc = read_json(path("targets.json"), {}) or {}
    prev = [w["ticker"] for w in prev_doc.get("weights", [])] if "screener" in prev_doc.get("source", "") else []
    top = int(robo["top"])
    picks = [t for t in prev if t in passed][:top]
    picks += [t for t in passed if t not in picks][:top - len(picks)]
    if not picks:
        log("nenhuma empresa passou nas regras de trader/rules.json; sigo com as ações anteriores")
        return False
    write_json(path("targets.json"), {
        "source": "trader-screener: %s (%s), %d de %d empresas passaram"
                  % (res.get("name", "rules"), today, len(passed), len(universe)),
        "as_of": today,
        "weights": [{"ticker": t, "name": names.get(t, ""), "weight": round(100.0 / len(picks), 4)}
                    for t in picks]})
    if set(picks) != set(prev):
        log("ações escolhidas pelo screener: %s (antes: %s)" % (", ".join(picks), ", ".join(prev) or "nenhuma"))
        return True
    log("screener refeito: as mesmas ações continuam passando (%s)" % ", ".join(picks))
    return False


def refresh_13f(robo, state, today, log):
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
    return held, positions


def check_stops(robo, positions, state, today, log):
    """Keep one trader-alerts stop rule per position and turn the ones that ring into sell orders."""
    pct = float(robo.get("stop_loss_pct") or 0)
    rules_doc = read_json(path("alerts.json"), {"rules": []}) or {"rules": []}
    rules = [r for r in rules_doc.get("rules", []) if not str(r.get("id", "")).startswith(STOP_PREFIX)]
    if pct > 0:
        for p in positions:
            rules.append({"id": STOP_PREFIX + p["symbol"], "symbol": p["symbol"], "type": "stop",
                          "price": round(float(p["avg_entry_price"]) * (1 - pct / 100.0), 2),
                          "note": "robô: -%g%% do preço médio" % pct})
    write_json(path("alerts.json"), dict(rules_doc, rules=rules))
    if pct <= 0 or not positions:
        return []
    res = skill_json("trader-alerts/alerts.py", "check", "--no-write")
    if res is None:
        log("não consegui checar os stops nesta rodada (trader-alerts)")
        return []
    qty = {p["symbol"]: p["qty"] for p in positions}
    orders = []
    until = (dt.date.fromisoformat(today) + dt.timedelta(days=int(robo["stop_cooldown_days"]))).isoformat()
    for ring in res.get("rings", []):
        sym = ring.get("symbol")
        if not str(ring.get("id", "")).startswith(STOP_PREFIX) or sym not in qty:
            continue
        orders.append({"symbol": sym, "side": "sell", "qty": qty[sym], "type": "market", "time_in_force": "day",
                       "why": "stop: %s" % ring["message"]})
        state.setdefault("cooldown", {})[sym] = until
        log("STOP acionado em %s: %s Fica fora da carteira até %s." % (sym, ring["message"], until))
    return orders


def active_targets(targets, state, today):
    """targets.json minus the stocks in stop cooldown; budget and rebalance renormalise the rest."""
    cool = {s: d for s, d in (state.get("cooldown") or {}).items() if d > today}
    state["cooldown"] = cool
    doc = dict(targets, weights=[w for w in targets.get("weights", []) if w["ticker"] not in cool])
    write_json(path("state", "targets_active.json"), doc)
    return "trader/state/targets_active.json"


def robo_strategy(state):
    return state.get("strategy", "screener")


def daily_report(cfg, targets, positions, state, today, log):
    """Once a day: portfolio, news on the tickers (trader-news) and Congress trades (trader-pelosi)."""
    if state.get("last_report") == today:
        return
    tickers = sorted({p["symbol"] for p in positions} | {w["ticker"] for w in targets.get("weights", [])})
    invested = sum(float(p.get("market_value") or 0) for p in positions)
    out = ["# Relatório do robô, %s" % today, "",
           "## Carteira", "",
           "Valor total: **$%.2f** (começou com $%.2f). Em ações: $%.2f. Caixa do robô: $%.2f."
           % (invested + state["cash"], float(cfg.get("budget", 0)), invested, state["cash"]), ""]
    if positions:
        out += ["| Ação | Quantidade | Valor | Resultado |", "| --- | --- | --- | --- |"]
        for p in sorted(positions, key=lambda p: -float(p.get("market_value") or 0)):
            out.append("| %s | %.4f | $%.2f | %+.1f%% |" % (p["symbol"], float(p["qty"]), float(p.get("market_value") or 0),
                                                         100 * float(p.get("unrealized_plpc") or 0)))
    cool = state.get("cooldown") or {}
    if cool:
        out += ["", "Fora da carteira por stop: %s." % ", ".join("%s até %s" % kv for kv in sorted(cool.items()))]

    short = read_json(path("shortlist.json"))
    if robo_strategy(state) == "screener" and short:
        metrics = {c["ticker"]: c for c in (read_json(path("metrics.json"), {}) or {}).get("companies", [])}
        picked = {w["ticker"] for w in targets.get("weights", [])}
        out += ["", "## Por que essas ações (trader-analyst + trader-screener)", "",
                "Regras: %s. %d passaram, %d não passaram."
                % (short.get("name", ""), len(short.get("passed", [])), len(short.get("failed", []))), "",
                "| Ação | Na carteira | Receita (cresc. 12m) | Margem operacional | P/L | Nota |",
                "| --- | --- | --- | --- | --- | --- |"]
        for r in short.get("passed", [])[:10]:
            m = metrics.get(r["ticker"], {})
            px = ((read_json(path("state", "universe_prices.json"), {}) or {}).get("prices") or {}).get(r["ticker"])
            pe = "%.1f" % (px / m["eps_ttm"]) if px and (m.get("eps_ttm") or 0) > 0 else "?"
            out.append("| %s | %s | %s%% | %s%% | %s | %s |" % (
                r["ticker"], "sim" if r["ticker"] in picked else "", m.get("revenue_growth_yoy", "?"),
                m.get("operating_margin", "?"), pe, r.get("score", "")))

    out += ["", "## Notícias (últimas 24 h)", ""]
    news = skill_json("trader-news/news.py", "--tickers", *(tickers + ["--hours", "24"]))
    if news is None:
        out.append("Não consegui ler as notícias hoje.")
    elif not news.get("items"):
        out.append("Nada novo sobre %s." % ", ".join(tickers))
    for it in (news or {}).get("items", [])[:20]:
        out.append("- **%s** %s: [%s](%s)%s" % (it.get("ticker", ""), "8-K na SEC" if it.get("kind") != "headline"
                                                 else it.get("source", ""), it.get("title", ""), it.get("link", ""),
                                                 " _(%s)_" % ", ".join(it["tags"]) if it.get("tags") else ""))

    out += ["", "## Trades de políticos (Câmara dos EUA)", ""]
    since = (dt.date.fromisoformat(today) - dt.timedelta(days=3)).isoformat()
    pdfs = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "ptr-pdfs")
    ptr = skill_json("trader-pelosi/ptr.py", "new", "--read", "--since", since, "--pdf-dir", pdfs,
                     "--tickers", *tickers)
    if ptr is None:
        out.append("Não consegui ler as declarações hoje.")
    elif not ptr.get("new"):
        out.append("Nenhuma declaração nova.")
    else:
        mine, other = [], []
        for rep in ptr.get("reports", []):
            who = rep["filing"]["name"].replace("Hon. ", "")
            for r in (rep.get("report") or {}).get("rows", []):
                line = "%s: %s %s%s, %s (negócio em %s)" % (
                    who, TXN_TYPES.get(r.get("type"), r.get("type")), r.get("asset", ""),
                    " (%s)" % r["ticker"] if r.get("ticker") else "", r.get("amount", ""), r.get("date", "?"))
                (mine if r.get("ticker") in tickers else other).append(line)
        out.append("%d declaração(ões) nova(s)." % len(ptr["new"]))
        if not mine and not other:
            out += ["- %s, entregue em %s: [PDF](%s)" % (f["name"].replace("Hon. ", ""), f["filed"], f["url"])
                    for f in ptr["new"][:25]]
        if mine:
            out += ["", "**Em ações que o robô tem ou segue:**"] + ["- " + l for l in mine]
        if other:
            out += ["", "Outras:"] + ["- " + l for l in other[:25]]
            if len(other) > 25:
                out.append("- e mais %d" % (len(other) - 25))
    rel = path("relatorios", today + ".md")
    os.makedirs(os.path.dirname(rel), exist_ok=True)
    with open(rel, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n\n_Não é recomendação de investimento._\n")
    state["last_report"] = today
    log("relatório do dia: trader/relatorios/%s.md" % today)


def propose(robo, held, new_13f, cash, targets_path):
    """Write trader/orders.json via budget (first buy) or rebalance. Returns the orders."""
    if os.path.exists(path("orders.json")):
        os.remove(path("orders.json"))
    if not held:
        skill("trader-budget/size.py", "--targets", targets_path, "--budget", "%.2f" % cash,
              "--prices", "trader/prices.json", "--write")
    else:
        args = ["--targets", targets_path, "--band", str(robo["band"]), "--write"]
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
    state["strategy"] = robo.get("strategy", "screener")
    new_13f = refresh_targets(api, robo, state, today, log)
    targets = read_json(path("targets.json"))
    if not targets:
        raise RoboError("sem trader/targets.json; o 13F não pôde ser lido")
    held, positions = sync_portfolio(api, state, targets)
    if robo.get("daily_report"):
        daily_report(cfg, targets, positions, state, today, log)
    if state["pending"]:
        log("%d ordem(ns) ainda abertas; espero a próxima rodada" % len(state["pending"]))
        return finish(state, log)

    orders = check_stops(robo, positions, state, today, log)
    if orders:
        write_json(path("orders.json"), {"created_by": "robo (stop)", "paper": True, "orders": orders})
    else:
        orders = propose(robo, held, new_13f, state["cash"], active_targets(targets, state, today))
        # rebalance rounds share counts to 4 decimals; never sell more than Alpaca says we hold
        have = {p["symbol"]: p["qty"] for p in positions}
        for o in orders:
            if o["side"] == "sell" and "qty" in o and float(o["qty"]) > float(have.get(o["symbol"], 0)):
                o["qty"] = have.get(o["symbol"], "0")
        if orders:
            doc = read_json(path("orders.json"), {})
            write_json(path("orders.json"), dict(doc, orders=orders))
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
