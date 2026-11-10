"""Pairs sleeve of the MyTrader robot: buy the laggard of two stocks that usually move together.

The rule is the most robust one from bot/pares.py (trader/backtest/pares.md): for each pair (A, B),
z = z-score over `lookback` days of log(A) - log(B). When |z| >= z_in and the pair's 120-day daily
return correlation is at least min_corr, buy the stock that fell behind (B when z is high, A when z
is low). Sell when the spread closes (z back inside z_out on that side) or after max_days. At most
`slots` positions, a stock held for one pair at a time; entries only from entry_after New York time,
so the signal is read close to the close as in the backtest.

Own cash ledger in state["pares"]; orders go through trader-risk with the sleeve's own files under
trader/pares/.
"""

import datetime as dt
import json
import math
import time

PARES_DEFAULTS = {
    "enabled": True,
    "pct": 30,
    "pairs": [["KO", "PEP"], ["V", "MA"], ["HD", "LOW"], ["XOM", "CVX"], ["JPM", "BAC"], ["GS", "MS"],
              ["UPS", "FDX"], ["MCD", "YUM"], ["WMT", "TGT"], ["PG", "CL"], ["CAT", "DE"], ["T", "VZ"],
              ["UNP", "CSX"], ["LMT", "NOC"], ["PFE", "MRK"], ["COST", "WMT"], ["MSFT", "AAPL"],
              ["C", "WFC"], ["COP", "XOM"]],
    "lookback": 20,
    "z_in": 1.5,
    "z_out": 0.0,
    "min_corr": 0.5,
    "max_days": 20,
    "slots": 5,
    "entry_after": "15:00",
}


def settings(robo):
    return dict(PARES_DEFAULTS, **(robo.get("pares") or {}))


def symbols(c):
    return {s.upper() for p in c["pairs"] for s in p}


def setup(state, cfg, robo, log, notify):
    """First run with the sleeve on: move pct of the starting budget out of the long-term ledger."""
    c = settings(robo)
    if not c["enabled"] or "pares" in state:
        return
    amount = round(float(cfg.get("budget", 0)) * float(c["pct"]) / 100.0, 2)
    state["cash"] = round(state["cash"] - amount, 2)
    state["pares"] = {"cash": amount, "start": amount, "positions": {}, "exited": {}}
    log("pares ligado: $%.2f (%g%%) separados do longo prazo, que vende um pouco do índice para cobrir"
        % (amount, c["pct"]))
    notify("Estratégia dos pares ligada", "$%.2f (%g%% do robô) vão comprar a ação que ficou para trás em pares "
           "como Coca-Cola e Pepsi. O longo prazo vende um pouco do índice para liberar esse dinheiro."
           % (amount, c["pct"]), tags=["link"])


def pair_stats(closes_a, closes_b, lookback):
    """(z-score of the log spread over lookback, 120-day correlation of daily log returns)."""
    n = min(len(closes_a), len(closes_b))
    if n < max(lookback, 121):
        return None, None
    la = [math.log(x) for x in closes_a[-n:]]
    lb = [math.log(x) for x in closes_b[-n:]]
    sp = [x - y for x, y in zip(la, lb)][-lookback:]
    m = sum(sp) / lookback
    sd = math.sqrt(sum((x - m) ** 2 for x in sp) / lookback)
    z = (sp[-1] - m) / sd if sd > 0 else None
    ra = [la[i] - la[i - 1] for i in range(n - 120, n)]
    rb = [lb[i] - lb[i - 1] for i in range(n - 120, n)]
    ma, mb = sum(ra) / 120, sum(rb) / 120
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    corr = cov / math.sqrt(va * vb) if va > 0 and vb > 0 else None
    return z, corr


def run(api, robo, cfg, state, today, ny_time, now, log, notify, skill_json, read_json, write_json, path,
        dry_run=False):
    c = settings(robo)
    st = state.get("pares")
    if not c["enabled"] or st is None:
        return
    if any(r.get("sleeve") == "pares" for r in state.get("pending", [])):
        log("pares: ordem ainda aberta; espero a próxima rodada")
        return
    syms = sorted(symbols(c))
    # leftovers under $1 (rounding dust from earlier sales) are not positions of this sleeve
    held = {p["symbol"]: p for p in api.positions()
            if p["symbol"] in syms and abs(float(p.get("market_value") or 0)) >= 1}
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=250)).isoformat()
    bars = api.bars(syms, start)
    last = api.latest_prices(syms)
    closes = {}
    for s in syms:
        xs = [float(b["c"]) for b in sorted(bars.get(s) or [], key=lambda b: b["t"]) if b["t"][:10] < today]
        if last.get(s):
            xs.append(last[s])
        closes[s] = xs
    signals = {}
    for a, b in c["pairs"]:
        z, corr = pair_stats(closes[a], closes[b], c["lookback"])
        signals["%s/%s" % (a, b)] = {"z": round(z, 2) if z is not None else None,
                                     "corr": round(corr, 2) if corr is not None else None}
    st["signals"] = signals

    orders = []
    for s, p in held.items():
        info = st["positions"].get(s) or {}
        pair = info.get("pair")
        g = signals.get(pair or "", {})
        since = info.get("date", today)
        days = (dt.date.fromisoformat(today) - dt.date.fromisoformat(since)).days
        why = None
        if pair and g.get("z") is not None:
            a, b = pair.split("/")
            if (s == b and g["z"] <= c["z_out"]) or (s == a and g["z"] >= -c["z_out"]):
                why = "a distância de %s voltou ao normal (z %.2f)" % (pair, g["z"])
        if not why and days >= c["max_days"]:
            why = "prazo máximo de %d dias" % c["max_days"]
        if why:
            orders.append({"symbol": s, "side": "sell", "qty": p["qty"], "type": "market", "time_in_force": "day",
                           "why": "pares, saída: " + why})

    if ny_time >= c["entry_after"]:
        blocked = (read_json(path("time", "sinais.json"), {}) or {}).get("bloqueio") or {}
        value = st["cash"] + sum(float(p.get("market_value") or 0) for p in held.values())
        slot, cash = value / c["slots"], st["cash"]
        busy_pairs = {(st["positions"].get(s) or {}).get("pair") for s in held}
        free = c["slots"] - len(held) + len(orders)
        cands = []
        for a, b in c["pairs"]:
            key = "%s/%s" % (a, b)
            g = signals[key]
            if key in busy_pairs or g["z"] is None or g["corr"] is None or g["corr"] < c["min_corr"]:
                continue
            buy = b if g["z"] >= c["z_in"] else a if g["z"] <= -c["z_in"] else None
            if not buy or buy in held or buy in blocked or st.get("exited", {}).get(buy) == today \
                    or any(o["symbol"] == buy for o in orders):
                continue
            cands.append((-abs(g["z"]), key, buy))
        for _, key, s in sorted(cands)[:max(free, 0)]:
            notional = math.floor(min(slot, cash))
            if notional < 5:
                break
            cash -= notional
            other = [x for x in key.split("/") if x != s][0]
            orders.append({"symbol": s, "side": "buy", "notional": str(notional), "type": "market",
                           "time_in_force": "day", "_pair": key,
                           "why": "pares, entrada: %s ficou para trás de %s (z %.2f, correlação %.2f)"
                                  % (s, other, signals[key]["z"], signals[key]["corr"])})
    if not orders:
        return

    write_json(path("pares", "portfolio.json"), {
        "cash": st["cash"], "source": "pares: ações dos pares + caixa dos pares",
        "positions": [{"ticker": s, "shares": float(p["qty"])} for s, p in held.items()]})
    write_json(path("pares", "prices.json"), {"as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "alpaca",
                                              "prices": dict(last)})
    write_json(path("pares", "orders.json"), {"created_by": "robo (pares)", "paper": True,
                                              "orders": [{k: v for k, v in o.items() if k != "_pair"} for o in orders]})
    write_json(path("pares", "config.json"), {
        "budget": st["start"], "cash_buffer_pct": 0, "max_position_pct": 100.0 / c["slots"] + 10,
        "max_order_usd": max(st["start"], st["cash"]), "min_order_usd": 5, "max_orders_per_day": 20,
        "fractional": True, "broker": "alpaca", "paper": True, "blocked": [],
        "allow_options": False, "allow_margin": False, "allow_short": False})
    res = skill_json("trader-risk/check.py", "--orders", "trader/pares/orders.json",
                     "--config", "trader/pares/config.json", "--portfolio", "trader/pares/portfolio.json",
                     "--prices", "trader/pares/prices.json", "--state", "trader/state/broker_pares.json",
                     "--out", "trader/state/risk_pares.json", "--today", today)
    broker = read_json(path("state", "broker_pares.json")) or {"orders": []}
    for v in (res or {}).get("results", []):
        o = orders[v["index"] - 1]
        if v["verdict"] != "PASS":
            log("pares, BLOQUEADA pelo risco: %s %s (%s)" % (o["side"], o["symbol"], "; ".join(v["reasons"])))
            continue
        body = {k: o[k] for k in ("symbol", "side", "type", "time_in_force", "qty", "notional") if k in o}
        if dry_run:
            log("[simulação] pares enviaria %s" % json.dumps(body))
            continue
        sent = api.submit(body)
        state["pending"].append({"id": sent["id"], "symbol": o["symbol"], "side": o["side"], "sleeve": "pares",
                                 "why": o["why"][:160]})
        broker["orders"].append({"date": today, "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "placed",
                                 "symbol": o["symbol"], "side": o["side"], "id": sent["id"],
                                 "approved_by": "robo (automatic, paper)", "why": o["why"]})
        if o["side"] == "buy":
            st["positions"][o["symbol"]] = {"date": today, "pair": o["_pair"]}
        else:
            st["positions"].pop(o["symbol"], None)
            st.setdefault("exited", {})[o["symbol"]] = today
        log("pares, enviada: %s %s %s (%s)" % (o["side"], o["symbol"],
                                              "$" + o["notional"] if "notional" in o else o["qty"] + " ações",
                                              o["why"]))
        if o["side"] == "sell":
            time.sleep(2)
    if not dry_run:
        cutoff = (now - dt.timedelta(days=30)).strftime("%Y-%m-%d")
        broker["orders"] = [r for r in broker["orders"] if r.get("date", "") >= cutoff]
        write_json(path("state", "broker_pares.json"), broker)
        st["exited"] = {s: d for s, d in st.get("exited", {}).items() if d >= cutoff}


def summary(api, state, robo):
    """The sleeve's value and positions, for the dashboard and the report."""
    st = state.get("pares")
    if not st:
        return None
    syms = symbols(settings(robo))
    pos = []
    for p in api.positions():
        if p["symbol"] not in syms or abs(float(p.get("market_value") or 0)) < 1:
            continue
        info = st["positions"].get(p["symbol"]) or {}
        pos.append({"symbol": p["symbol"], "qty": float(p["qty"]), "avg": float(p["avg_entry_price"]),
                    "price": float(p["current_price"]), "value": round(float(p["market_value"]), 2),
                    "pl": round(float(p["market_value"]) - float(p["cost_basis"]), 2),
                    "pl_pct": round(100 * float(p.get("unrealized_plpc") or 0), 2),
                    "since": info.get("date"), "pair": info.get("pair")})
    value = round(st["cash"] + sum(p["value"] for p in pos), 2)
    return {"start": st["start"], "cash": st["cash"], "value": value,
            "change": round(value - st["start"], 2),
            "change_pct": round(100 * (value / st["start"] - 1), 2) if st["start"] else 0, "positions": pos}
