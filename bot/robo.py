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
  8. the short-term sleeve (bot/curto.py) trades ETF dips with its own cash, when "curto" is on

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

import curto

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILLS = os.path.join(ROOT, ".claude", "skills")
T = os.path.join(ROOT, "trader")
PANEL = os.path.join(ROOT, "painel")
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
    "take_profit_pct": 10,
    "take_profit_reentry_dip_pct": 5,
    "take_profit_max_days": 10,
    "daily_report": True,
}
STOP_PREFIX = "robo-stop-"
TAKE_PREFIX = "robo-take-"
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

    def bars(self, symbols, start):
        """Daily bars per symbol since start (YYYY-MM-DD), split-adjusted, following page tokens."""
        out, token = {}, None
        while True:
            url = (QUOTE_URL + "/v2/stocks/bars?timeframe=1Day&adjustment=all&feed=iex&limit=10000&start=%s&symbols=%s"
                   % (start, ",".join(sorted(symbols))) + ("&page_token=" + token if token else ""))
            doc = self.call("GET", url) or {}
            for sym, bars in (doc.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            token = doc.get("next_page_token")
            if not token:
                return out

    def latest_prices(self, symbols):
        if not symbols:
            return {}
        url = QUOTE_URL + "/v2/stocks/trades/latest?feed=iex&symbols=" + ",".join(sorted(symbols))
        trades = (self.call("GET", url) or {}).get("trades", {})
        return {s: float(t["p"]) for s, t in trades.items() if t.get("p")}


def notify(title, message, tags=(), click=None, priority=3):
    """Push to the phone through ntfy (https://ntfy.sh) when NTFY_TOPIC is set; never fails the run."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    if os.environ.get("ROBO_DRY_RUN"):
        title = "[simulação] " + title
    body = {"topic": topic, "title": title, "message": message, "tags": list(tags), "priority": priority}
    if click:
        body["click"] = click
    server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
    req = urllib.request.Request(server.rstrip("/") + "/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).close()
    except (urllib.error.URLError, OSError) as e:
        print("aviso: notificação não enviada (%s)" % e, file=sys.stderr)


def github_link(rel):
    repo = os.environ.get("GITHUB_REPOSITORY")
    return "https://github.com/%s/blob/main/%s" % (repo, rel) if repo else None


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
            book = state["curto"] if rec.get("sleeve") == "curto" else state
            book["cash"] = round(book["cash"] + (amount if o["side"] == "sell" else -amount), 2)
            label = "curto prazo" if rec.get("sleeve") == "curto" else "longo prazo"
            log("%s %s (%s): %s, %s ações a $%.2f (caixa do %s: $%.2f)"
                % (o["side"], o["symbol"], label, o["status"], qty, px, label, book["cash"]))
            if qty > 0:
                state.setdefault("fills", []).append({
                    "at": str(o.get("filled_at") or dt.datetime.now(dt.timezone.utc).isoformat())[:19] + "Z",
                    "symbol": o["symbol"], "side": o["side"], "qty": round(qty, 6), "price": px,
                    "amount": amount, "why": rec.get("why", ""), "sleeve": rec.get("sleeve", "longo")})
                state["fills"] = state["fills"][-100:]
            verb = "Compra" if o["side"] == "buy" else "Venda"
            if qty > 0:
                notify("%s de %s executada (%s)" % (verb, o["symbol"], label),
                       "%.4f ações a $%.2f = $%.2f\nCaixa do %s: $%.2f (conta paper)"
                       % (qty, px, amount, label, book["cash"]),
                       tags=["chart_with_upwards_trend" if o["side"] == "buy" else "moneybag"])
            else:
                notify("%s de %s não executada" % (verb, o["symbol"]), "Status na Alpaca: %s" % o["status"],
                       tags=["warning"])
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


def long_positions(api, robo):
    """Alpaca positions that belong to the long-term sleeve (the short-term one only holds its ETFs)."""
    c = curto.settings(robo)
    etfs = set(u.upper() for u in c["universe"]) if c["enabled"] else set()
    return [p for p in api.positions() if p["symbol"] not in etfs]


def sync_portfolio(api, robo, state, targets):
    positions = long_positions(api, robo)
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
    """Keep one trader-alerts stop rule and one take-profit target per position;
    the ones that ring become sell orders for the whole position."""
    pct = float(robo.get("stop_loss_pct") or 0)
    take = float(robo.get("take_profit_pct") or 0)
    rules_doc = read_json(path("alerts.json"), {"rules": []}) or {"rules": []}
    rules = [r for r in rules_doc.get("rules", [])
             if not str(r.get("id", "")).startswith((STOP_PREFIX, TAKE_PREFIX))]
    for p in positions:
        avg = float(p["avg_entry_price"])
        if pct > 0:
            rules.append({"id": STOP_PREFIX + p["symbol"], "symbol": p["symbol"], "type": "stop",
                          "price": round(avg * (1 - pct / 100.0), 2), "note": "robô: -%g%% do preço médio" % pct})
        if take > 0:
            rules.append({"id": TAKE_PREFIX + p["symbol"], "symbol": p["symbol"], "type": "target",
                          "price": round(avg * (1 + take / 100.0), 2), "note": "robô: +%g%% do preço médio" % take})
    write_json(path("alerts.json"), dict(rules_doc, rules=rules))
    if (pct <= 0 and take <= 0) or not positions:
        return []
    res = skill_json("trader-alerts/alerts.py", "check", "--no-write")
    if res is None:
        log("não consegui checar os stops nesta rodada (trader-alerts)")
        return []
    qty = {p["symbol"]: p["qty"] for p in positions}
    pos = {p["symbol"]: p for p in positions}
    orders = []
    until = (dt.date.fromisoformat(today) + dt.timedelta(days=int(robo["stop_cooldown_days"]))).isoformat()
    for ring in res.get("rings", []):
        sym = ring.get("symbol")
        if sym in qty and str(ring.get("id", "")).startswith(TAKE_PREFIX) and not any(o["symbol"] == sym for o in orders):
            p = pos[sym]
            price, avg = float(p["current_price"]), float(p["avg_entry_price"])
            back = (dt.date.fromisoformat(today) + dt.timedelta(days=int(robo["take_profit_max_days"]))).isoformat()
            dip = float(robo.get("take_profit_reentry_dip_pct") or 0)
            gain = 100 * (price / avg - 1)
            orders.append({"symbol": sym, "side": "sell", "qty": qty[sym], "type": "market", "time_in_force": "day",
                           "why": "realizar lucro: %s" % ring["message"],
                           "_effect": {"key": "takeprofit", "value": {
                               "sold_at": price, "date": today, "until": back,
                               "rebuy_below": round(price * (1 - dip / 100.0), 2)},
                               "log": "LUCRO em %s: %+.1f%% sobre o preço médio $%.2f. Vende tudo; recompra abaixo "
                                      "de $%.2f ou em %s." % (sym, gain, avg, price * (1 - dip / 100.0), back),
                               "title": "Realizando lucro em %s (%+.1f%%)" % (sym, gain),
                               "message": "Preço $%.2f, preço médio $%.2f.\nO robô vende tudo e guarda o dinheiro "
                                          "para recomprar abaixo de $%.2f ou em %s."
                                          % (price, avg, price * (1 - dip / 100.0), back),
                               "tags": ["tada", "moneybag"]}})
            continue
        if not str(ring.get("id", "")).startswith(STOP_PREFIX) or sym not in qty:
            continue
        orders.append({"symbol": sym, "side": "sell", "qty": qty[sym], "type": "market", "time_in_force": "day",
                       "why": "stop: %s" % ring["message"],
                       "_effect": {"key": "cooldown", "value": until,
                                   "log": "STOP acionado em %s: %s Fica fora da carteira até %s."
                                          % (sym, ring["message"], until),
                                   "title": "Stop acionado em %s" % sym,
                                   "message": "%s\nO robô vai vender e deixar %s de fora até %s."
                                              % (ring["message"], sym, until),
                                   "tags": ["rotating_light"]}})
    return orders


def apply_effect(state, o, log):
    """What a stop or take-profit sale changes, booked only once its order was actually sent."""
    e = o.get("_effect")
    if not e:
        return
    state.setdefault(e["key"], {})[o["symbol"]] = e["value"]
    log(e["log"])
    notify(e["title"], e["message"], tags=e["tags"], priority=4)


def active_targets(api, cfg, targets, state, today, log):
    """targets.json minus the stocks in stop cooldown (budget and rebalance spread their weight over
    the rest) and minus the stocks sold for profit, whose weight is held back as extra cash buffer
    until they dip back (or the wait runs out). Returns (targets path, config path)."""
    cool = {s: d for s, d in (state.get("cooldown") or {}).items() if d > today}
    state["cooldown"] = cool
    waiting = state.get("takeprofit") or {}
    prices = api.latest_prices(list(waiting)) if waiting else {}
    for sym, tp in list(waiting.items()):
        px = prices.get(sym)
        if today >= tp["until"] or (px and px <= tp["rebuy_below"]):
            why = ("caiu para $%.2f" % px) if px and px <= tp["rebuy_below"] else "acabou o prazo de espera"
            log("recompra liberada em %s (%s; vendida a $%.2f)" % (sym, why, tp["sold_at"]))
            notify("Recomprando %s" % sym, "%s: %s. Vendida a $%.2f em %s."
                   % (sym, why, tp["sold_at"], tp["date"]), tags=["shopping_cart"])
            del waiting[sym]
    state["takeprofit"] = waiting
    weights = targets.get("weights", [])
    total = sum(float(w["weight"]) for w in weights) or 1
    held_back = sum(float(w["weight"]) for w in weights if w["ticker"] in waiting) / total
    doc = dict(targets, weights=[w for w in weights if w["ticker"] not in cool and w["ticker"] not in waiting])
    write_json(path("state", "targets_active.json"), doc)
    base = float(cfg.get("cash_buffer_pct") or 0)
    active = dict(cfg, cash_buffer_pct=round(base + (100 - base) * held_back, 2))
    write_json(path("state", "config_active.json"), active)
    return "trader/state/targets_active.json", "trader/state/config_active.json"


def robo_strategy(state):
    return state.get("strategy", "screener")


def daily_report(api, robo, cfg, targets, positions, state, today, log):
    """Once a day: portfolio, news on the tickers (trader-news) and Congress trades (trader-pelosi)."""
    if state.get("last_report") == today:
        return
    tickers = sorted({p["symbol"] for p in positions} | {w["ticker"] for w in targets.get("weights", [])})
    invested = sum(float(p.get("market_value") or 0) for p in positions)
    short_term = curto.summary(api, state, robo)
    st_value = short_term["value"] if short_term else 0
    out = ["# Relatório do robô, %s" % today, "",
           "## Carteira", "",
           "Valor total do robô: **$%.2f** (começou com $%.2f)." % (invested + state["cash"] + st_value,
                                                                  float(cfg.get("budget", 0))), "",
           "**Longo prazo:** $%.2f (em ações $%.2f, caixa $%.2f)." % (invested + state["cash"], invested, state["cash"]),
           ""]
    if positions:
        out += ["| Ação | Quantidade | Valor | Resultado |", "| --- | --- | --- | --- |"]
        for p in sorted(positions, key=lambda p: -float(p.get("market_value") or 0)):
            out.append("| %s | %.4f | $%.2f | %+.1f%% |" % (p["symbol"], float(p["qty"]), float(p.get("market_value") or 0),
                                                         100 * float(p.get("unrealized_plpc") or 0)))
    for sym, tp in sorted((state.get("takeprofit") or {}).items()):
        out += ["", "Lucro realizado em %s: vendida a $%.2f em %s; recompra abaixo de $%.2f ou em %s."
                % (sym, tp["sold_at"], tp["date"], tp["rebuy_below"], tp["until"])]
    cool = state.get("cooldown") or {}
    if cool:
        out += ["", "Fora da carteira por stop: %s." % ", ".join("%s até %s" % kv for kv in sorted(cool.items()))]
    if short_term:
        out += ["", "**Curto prazo (ETFs):** $%.2f (%+.1f%% desde $%.2f), caixa $%.2f."
                % (short_term["value"], short_term["change_pct"], short_term["start"], short_term["cash"])]
        for p in short_term["positions"]:
            out.append("- %s: $%.2f (%+.1f%%), desde %s" % (p["symbol"], p["value"], p["pl_pct"], p["since"] or "?"))

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
    total = invested + state["cash"] + st_value
    budget = float(cfg.get("budget", 0)) or total
    notify("Relatório do robô, %s" % today,
           "Carteira: $%.2f (%+.1f%% desde o início)\nAções: %s\nNotícias novas: %d\nToque para abrir o relatório."
           % (total, 100.0 * (total / budget - 1), ", ".join(sorted(p["symbol"] for p in positions)) or "nenhuma ainda",
              len((news or {}).get("items", []))),
           tags=["memo"], click=github_link("trader/relatorios/%s.md" % today))


def propose(robo, held, new_13f, cash, targets_path, config_path):
    """Write trader/orders.json via budget (first buy) or rebalance. Returns the orders."""
    if os.path.exists(path("orders.json")):
        os.remove(path("orders.json"))
    if not held:
        skill("trader-budget/size.py", "--targets", targets_path, "--config", config_path,
              "--budget", "%.2f" % cash, "--prices", "trader/prices.json", "--write")
    else:
        args = ["--targets", targets_path, "--config", config_path, "--band", str(robo["band"]), "--write"]
        if new_13f and robo.get("sell_untracked_on_new_13f"):
            args.append("--sell-untracked")
        skill("trader-rebalance/rebalance.py", *args)
    return (read_json(path("orders.json"), {}) or {}).get("orders", [])


SECRET_VARS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "SEC_USER_AGENT", "NTFY_TOPIC")


def clean_secrets():
    """Secrets pasted with a trailing Enter carry a newline, which urllib rejects in a header.
    Trim them here (the skill scripts inherit this environment) and fold inner whitespace."""
    for k in SECRET_VARS:
        if k in os.environ:
            os.environ[k] = " ".join(os.environ[k].split())


def run(dry_run=False):
    clean_secrets()
    if dry_run:
        os.environ["ROBO_DRY_RUN"] = "1"
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
        raise RoboError("sem trader/targets.json: não consegui escolher as ações nesta rodada (veja o erro acima)")
    curto.setup(state, cfg, robo, log, notify)
    held, positions = sync_portfolio(api, robo, state, targets)
    panel = (lambda: write_panel(api, robo, cfg, state, targets, now)) if not dry_run else None
    if robo.get("daily_report"):
        daily_report(api, robo, cfg, targets, positions, state, today, log)
    ny_time = str(clock["timestamp"])[11:16]
    curto.run(api, robo, cfg, state, today, ny_time, now, log, notify, skill_json, read_json, write_json, path,
              dry_run=dry_run)
    long_pending = [r for r in state["pending"] if r.get("sleeve") != "curto"]
    if long_pending:
        log("%d ordem(ns) do longo prazo ainda abertas; espero a próxima rodada" % len(long_pending))
        return finish(state, log, panel=panel, save=not dry_run)

    orders = check_stops(robo, positions, state, today, log)
    config_path = "trader/config.json"
    if orders:
        write_json(path("orders.json"), {"created_by": "robo (stop)", "paper": True,
                                         "orders": [{k: v for k, v in o.items() if k != "_effect"} for o in orders]})
    else:
        targets_path, config_path = active_targets(api, cfg, targets, state, today, log)
        orders = propose(robo, held, new_13f, state["cash"], targets_path, config_path)
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
        if not dry_run and state["pending"]:
            time.sleep(5)
            settle(api, state, log)
        return finish(state, log, panel=panel, save=not dry_run)

    code, out = skill("trader-risk/check.py", "--today", today, "--config", config_path, "--json")
    verdicts = json.loads(out)["results"] if out.strip() else []
    broker = read_json(path("state", "broker.json")) or {"orders": []}
    passed = []
    for v in verdicts:
        o = orders[v["index"] - 1]
        if v["verdict"] == "PASS":
            passed.append(o)
        else:
            log("BLOQUEADA pelo risco: %s %s %s (%s)%s" % (o["side"], o["symbol"], v["amount"],
                                                          "; ".join(v["reasons"]),
                                                          "; tento de novo na próxima rodada" if o.get("_effect") else ""))
    # sells first so their cash is back before the buys
    for o in sorted(passed, key=lambda o: o["side"] != "sell"):
        body = {k: o[k] for k in ("symbol", "side", "type", "time_in_force", "qty", "notional") if k in o}
        if dry_run:
            log("[simulação] enviaria %s" % json.dumps(body))
            continue
        sent = api.submit(body)
        apply_effect(state, o, log)
        state["pending"].append({"id": sent["id"], "symbol": o["symbol"], "side": o["side"],
                                 "why": o.get("why", "")[:160]})
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
    return finish(state, log, save=not dry_run, panel=panel)


def write_panel(api, robo, cfg, state, targets, now):
    """painel/dados.json for the phone dashboard (GitHub Pages): balance, positions, history, picks."""
    positions = long_positions(api, robo)
    invested = sum(float(p.get("market_value") or 0) for p in positions)
    short_term = curto.summary(api, state, robo)
    total = round(invested + state["cash"] + (short_term["value"] if short_term else 0), 2)
    start = float(cfg.get("budget", 0)) or total
    hist = read_json(path("state", "historico.json"), []) or []
    hist.append({"t": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "v": total})
    hist = hist[-3000:]
    write_json(path("state", "historico.json"), hist)
    names = {w["ticker"]: w.get("name", "") for w in targets.get("weights", [])}
    pos = []
    for p in sorted(positions, key=lambda p: -float(p.get("market_value") or 0)):
        value = float(p.get("market_value") or 0)
        pos.append({"symbol": p["symbol"], "name": names.get(p["symbol"], ""), "qty": float(p["qty"]),
                    "avg": float(p["avg_entry_price"]), "price": float(p["current_price"]), "value": round(value, 2),
                    "pl": round(value - float(p["cost_basis"]), 2), "pl_pct": round(100 * float(p.get("unrealized_plpc") or 0), 2),
                    "weight": round(100 * value / total, 2) if total else 0})
    picks = []
    short = read_json(path("shortlist.json"), {}) or {}
    metrics = {c["ticker"]: c for c in (read_json(path("metrics.json"), {}) or {}).get("companies", [])}
    uprices = (read_json(path("state", "universe_prices.json"), {}) or {}).get("prices") or {}
    for r in short.get("passed", [])[:10]:
        m = metrics.get(r["ticker"], {})
        px, eps = uprices.get(r["ticker"]), m.get("eps_ttm") or 0
        picks.append({"symbol": r["ticker"], "name": r.get("name", ""), "score": r.get("score"),
                      "growth": m.get("revenue_growth_yoy"), "margin": m.get("operating_margin"),
                      "pe": round(px / eps, 1) if px and eps > 0 else None,
                      "held": r["ticker"] in names})
    reports = sorted(f for f in os.listdir(path("relatorios"))) if os.path.isdir(path("relatorios")) else []
    write_json(os.path.join(PANEL, "dados.json"), {
        "as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "paper": True,
        "strategy": state.get("strategy"), "rules": short.get("name"),
        "start": start, "total": total, "invested": round(invested, 2), "cash": state["cash"],
        "change": round(total - start, 2), "change_pct": round(100 * (total / start - 1), 2) if start else 0,
        "positions": pos, "targets": targets.get("weights", []), "cooldown": state.get("cooldown") or {},
        "takeprofit": state.get("takeprofit") or {}, "curto": short_term,
        "long_value": round(invested + state["cash"], 2),
        "pending": len(state.get("pending", [])), "fills": list(reversed(state.get("fills", [])))[:30],
        "picks": picks, "history": hist,
        "report": "trader/relatorios/" + reports[-1] if reports else None,
        "repo": os.environ.get("GITHUB_REPOSITORY")})


def finish(state, log, save=True, panel=None):
    if save:
        write_json(path("state", "robo.json"), state)
    if panel:
        try:
            panel()
        except (RoboError, OSError, KeyError, ValueError) as e:
            print("aviso: painel não atualizado (%s)" % e, file=sys.stderr)
    if log.lines:
        log.save()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="MyTrader paper-trading robot.")
    ap.add_argument("--dry-run", action="store_true", help="do everything except sending orders")
    a = ap.parse_args(argv)
    try:
        return run(a.dry_run)
    except Exception as e:
        print("erro: %s" % e, file=sys.stderr)
        run_url = os.environ.get("GITHUB_RUN_ID") and "https://github.com/%s/actions/runs/%s" % (
            os.environ.get("GITHUB_REPOSITORY"), os.environ["GITHUB_RUN_ID"])
        notify("Robô com erro", str(e)[:400], tags=["x"], click=run_url or None, priority=4)
        if isinstance(e, RoboError):
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
