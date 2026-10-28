"""Short-term sleeve of the MyTrader robot: RSI(2) dip-buying on liquid ETFs.

Rules, on daily closes with the latest price standing in for today's close:
  enter  price above its 200-day average (up-trend) and RSI(2) under rsi_entry (a sharp 2-3 day dip);
         only from entry_after New York time, so the signal is read close to the close
  exit   price back above its 5-day average (the bounce), or stop_pct under the entry, or max_days held

The sleeve trades ETFs only, so its positions never mix with the long-term stocks in the same
Alpaca account. It keeps its own cash ledger in state["curto"], and every order still goes
through trader-risk with the sleeve's own config, portfolio and daily log.
"""

import datetime as dt
import json
import math
import os
import time

CURTO_DEFAULTS = {
    "enabled": True,
    "pct": 20,
    "universe": ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP",
                 "XLI", "XLU", "XLB", "SMH"],
    "max_positions": 4,
    "rsi_entry": 10,
    "exit_sma": 5,
    "trend_sma": 200,
    "stop_pct": 8,
    "max_days": 10,
    "entry_after": "15:00",
}


def settings(robo):
    return dict(CURTO_DEFAULTS, **(robo.get("curto") or {}))


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def rsi(xs, n=2):
    """Wilder's RSI over the whole series; None when there are too few closes."""
    if len(xs) <= n:
        return None
    diffs = [b - a for a, b in zip(xs, xs[1:])]
    gain = sum(max(d, 0) for d in diffs[:n]) / n
    loss = sum(max(-d, 0) for d in diffs[:n]) / n
    for d in diffs[n:]:
        gain = (gain * (n - 1) + max(d, 0)) / n
        loss = (loss * (n - 1) + max(-d, 0)) / n
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def setup(state, cfg, robo, log, notify):
    """First run with the sleeve on: move pct of the starting budget out of the long-term ledger."""
    c = settings(robo)
    if not c["enabled"] or "curto" in state:
        return
    amount = round(float(cfg.get("budget", 0)) * float(c["pct"]) / 100.0, 2)
    state["cash"] = round(state["cash"] - amount, 2)
    state["curto"] = {"cash": amount, "start": amount, "positions": {}, "exited": {}}
    log("curto prazo ligado: $%.2f (%g%%) separados do longo prazo; o longo prazo vende um pouco de cada ação "
        "para cobrir" % (amount, c["pct"]))
    notify("Curto prazo ligado", "$%.2f (%g%% do robô) vão operar ETFs no curto prazo. O longo prazo vende um "
           "pouco de cada ação para liberar esse dinheiro." % (amount, c["pct"]), tags=["zap"])


def closes(api, universe, today):
    """Daily closes per symbol up to yesterday (today's bar, if any, is dropped)."""
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=420)).isoformat()
    out = {s: [] for s in universe}
    for s, bars in api.bars(universe, start).items():
        out[s] = [float(b["c"]) for b in sorted(bars, key=lambda b: b["t"]) if b["t"][:10] < today]
    return out


def run(api, robo, cfg, state, today, ny_time, now, log, notify, skill_json, read_json, write_json, path,
        dry_run=False):
    c = settings(robo)
    st = state.get("curto")
    if not c["enabled"] or st is None:
        return
    if any(r.get("sleeve") == "curto" for r in state.get("pending", [])):
        log("curto prazo: ordem ainda aberta; espero a próxima rodada")
        return
    universe = [u.upper() for u in c["universe"]]
    held = {p["symbol"]: p for p in api.positions() if p["symbol"] in universe}
    hist = closes(api, universe, today)
    last = api.latest_prices(universe)
    orders, signals = [], {}
    for s in universe:
        px = last.get(s)
        if not px or len(hist[s]) < c["trend_sma"]:
            continue
        xs = hist[s] + [px]
        signals[s] = {"price": px, "rsi2": rsi(xs), "sma_exit": sma(xs, c["exit_sma"]),
                      "sma_trend": sma(xs, c["trend_sma"])}

    for s, p in held.items():
        g = signals.get(s)
        if not g:
            continue
        entry = float(p["avg_entry_price"])
        since = (st["positions"].get(s) or {}).get("date", today)
        days = (dt.date.fromisoformat(today) - dt.date.fromisoformat(since)).days
        why = None
        if g["price"] <= entry * (1 - c["stop_pct"] / 100.0):
            why = "stop de %g%% (entrada $%.2f, agora $%.2f)" % (c["stop_pct"], entry, g["price"])
        elif g["price"] > g["sma_exit"]:
            why = "repique: $%.2f acima da média de %d dias ($%.2f)" % (g["price"], c["exit_sma"], g["sma_exit"])
        elif days >= c["max_days"]:
            why = "prazo máximo de %d dias" % c["max_days"]
        if why:
            orders.append({"symbol": s, "side": "sell", "qty": p["qty"], "type": "market", "time_in_force": "day",
                           "why": "curto prazo, saída: " + why})

    if ny_time >= c["entry_after"]:
        value = st["cash"] + sum(float(p.get("market_value") or 0) for p in held.values())
        slot = value / c["max_positions"]
        cash = st["cash"]
        free = c["max_positions"] - len(held) + len(orders)
        picks = sorted((g["rsi2"], s) for s, g in signals.items()
                       if s not in held and st.get("exited", {}).get(s) != today
                       and g["rsi2"] is not None and g["rsi2"] < c["rsi_entry"] and g["price"] > g["sma_trend"])
        for r, s in picks[:max(free, 0)]:
            notional = math.floor(min(slot, cash))
            if notional < 5:
                break
            cash -= notional
            orders.append({"symbol": s, "side": "buy", "notional": str(notional), "type": "market",
                           "time_in_force": "day",
                           "why": "curto prazo, entrada: RSI(2) %.1f, acima da média de %d dias ($%.2f)"
                                  % (r, c["trend_sma"], signals[s]["sma_trend"])})
    st["signals"] = {s: {k: (round(v, 2) if v is not None else None) for k, v in g.items()}
                     for s, g in signals.items()}
    if not orders:
        return

    # trader-risk with the sleeve's own files
    write_json(path("curto", "portfolio.json"), {
        "cash": st["cash"], "source": "curto prazo: posições em ETFs + caixa do curto prazo",
        "positions": [{"ticker": s, "shares": float(p["qty"])} for s, p in held.items()]})
    write_json(path("curto", "prices.json"), {"as_of": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "source": "alpaca",
                                              "prices": {s: g["price"] for s, g in signals.items()}})
    write_json(path("curto", "orders.json"), {"created_by": "robo (curto prazo)", "paper": True, "orders": orders})
    write_json(path("curto", "config.json"), {
        "budget": st["start"], "cash_buffer_pct": 0, "max_position_pct": 100.0 / c["max_positions"] + 10,
        "max_order_usd": max(st["start"], st["cash"]), "min_order_usd": 5, "max_orders_per_day": 20,
        "fractional": True, "broker": "alpaca", "paper": True, "blocked": [],
        "allow_options": False, "allow_margin": False, "allow_short": False})
    res = skill_json("trader-risk/check.py", "--orders", "trader/curto/orders.json",
                     "--config", "trader/curto/config.json", "--portfolio", "trader/curto/portfolio.json",
                     "--prices", "trader/curto/prices.json", "--state", "trader/state/broker_curto.json",
                     "--out", "trader/state/risk_curto.json", "--today", today)
    broker = read_json(path("state", "broker_curto.json")) or {"orders": []}
    for v in (res or {}).get("results", []):
        o = orders[v["index"] - 1]
        if v["verdict"] != "PASS":
            log("curto prazo, BLOQUEADA pelo risco: %s %s (%s)" % (o["side"], o["symbol"], "; ".join(v["reasons"])))
            continue
        body = {k: o[k] for k in ("symbol", "side", "type", "time_in_force", "qty", "notional") if k in o}
        if dry_run:
            log("[simulação] curto prazo enviaria %s" % json.dumps(body))
            continue
        sent = api.submit(body)
        state["pending"].append({"id": sent["id"], "symbol": o["symbol"], "side": o["side"], "sleeve": "curto",
                                 "why": o["why"][:160]})
        broker["orders"].append({"date": today, "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "status": "placed",
                                 "symbol": o["symbol"], "side": o["side"], "id": sent["id"],
                                 "approved_by": "robo (automatic, paper)", "why": o["why"]})
        if o["side"] == "buy":
            st["positions"][o["symbol"]] = {"date": today}
        else:
            st["positions"].pop(o["symbol"], None)
            st.setdefault("exited", {})[o["symbol"]] = today
        log("curto prazo, enviada: %s %s %s (%s)" % (o["side"], o["symbol"],
                                                    "$" + o["notional"] if "notional" in o else o["qty"] + " cotas",
                                                    o["why"]))
        if o["side"] == "sell":
            time.sleep(2)
    if not dry_run:
        cutoff = (now - dt.timedelta(days=30)).strftime("%Y-%m-%d")
        broker["orders"] = [r for r in broker["orders"] if r.get("date", "") >= cutoff]
        write_json(path("state", "broker_curto.json"), broker)
        st["exited"] = {s: d for s, d in st.get("exited", {}).items() if d >= cutoff}


def summary(api, state, robo):
    """The sleeve's value and positions, for the dashboard and the report."""
    st = state.get("curto")
    if not st:
        return None
    universe = set(u.upper() for u in settings(robo)["universe"])
    pos = []
    for p in api.positions():
        if p["symbol"] not in universe:
            continue
        since = (st["positions"].get(p["symbol"]) or {}).get("date")
        pos.append({"symbol": p["symbol"], "qty": float(p["qty"]), "avg": float(p["avg_entry_price"]),
                    "price": float(p["current_price"]), "value": round(float(p["market_value"]), 2),
                    "pl": round(float(p["market_value"]) - float(p["cost_basis"]), 2),
                    "pl_pct": round(100 * float(p.get("unrealized_plpc") or 0), 2), "since": since})
    value = round(st["cash"] + sum(p["value"] for p in pos), 2)
    return {"start": st["start"], "cash": st["cash"], "value": value,
            "change": round(value - st["start"], 2),
            "change_pct": round(100 * (value / st["start"] - 1), 2) if st["start"] else 0,
            "positions": pos, "signals": st.get("signals", {})}
