#!/usr/bin/env python3
"""Backtest of the robot's rules on daily history, against simply holding SPY.

What is tested (decide on a day's close with data up to that day, earn the next day's return):
  hold      buy and hold SPY, dividends included
  filtro    the long-term market filter: SPY while SPY is above its 200-day average, the inverse
            of SPY (like SH: minus SPY's daily return, minus SH's 0.89% a year fee) once SPY closes
            2% under the average, back to SPY above it; "filtro_caixa" holds cash instead
  curto     the short-term sleeve: RSI(2) dip-buying on the ETFs in an up-trend and the inverse
            of QQQ/DIA/IWM after a sharp rally in a down-trend, 4 slots, exits on the 5-day
            average, an 8% stop or 10 days
  robo      80% filtro + 20% curto, rebalanced yearly (the robot's split)

The stock screener cannot be tested this way (no point-in-time fundamentals), so SPY stands in
for the long-term stocks. Trading costs: 0.05% per buy or sell. Results go to trader/backtest/.

    python3 bot/backtest.py              # downloads history (needs network)
"""

import datetime as dt
import json
import math
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "trader", "backtest")
ETFS = ["SPY", "QQQ", "IWM", "DIA", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLU", "XLB", "SMH"]
INVERSE_OF = ["SPY", "QQQ", "DIA", "IWM"]
COST = 0.0005
SH_FEE = 0.0089 / 252
START = "2007-01-01"
UA = "Mozilla/5.0 (compatible; mytrader-backtest/1.0)"


def yahoo(symbol):
    """Daily dividend-adjusted closes {date: close} from Yahoo's chart API."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?period1=0&period2=%d&interval=1d"
           "&events=div%%2Csplit&includeAdjustedClose=true" % (symbol, int(dt.datetime.now().timestamp())))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read())["chart"]["result"][0]
    adj = res["indicators"]["adjclose"][0]["adjclose"]
    out = {}
    for t, c in zip(res["timestamp"], adj):
        if c:
            out[dt.datetime.utcfromtimestamp(t).date().isoformat()] = float(c)
    return out


def rsi2(xs):
    if len(xs) < 3:
        return None
    n = 2
    diffs = [b - a for a, b in zip(xs, xs[1:])]
    gain = sum(max(d, 0) for d in diffs[:n]) / n
    loss = sum(max(-d, 0) for d in diffs[:n]) / n
    for d in diffs[n:]:
        gain = (gain * (n - 1) + max(d, 0)) / n
        loss = (loss * (n - 1) + max(-d, 0)) / n
    return 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)


def stats(curve, dates):
    """CAGR, total, max drawdown, worst calendar year for an equity curve (list of values)."""
    years = (dt.date.fromisoformat(dates[-1]) - dt.date.fromisoformat(dates[0])).days / 365.25
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    by_year = {}
    for d, v in zip(dates, curve):
        by_year.setdefault(d[:4], [v, v])[1] = v
    prev, yearly = curve[0], {}
    for y in sorted(by_year):
        yearly[y] = by_year[y][1] / prev - 1
        prev = by_year[y][1]
    return {"cagr": (curve[-1] / curve[0]) ** (1 / years) - 1, "total": curve[-1] / curve[0] - 1,
            "max_drawdown": mdd, "worst_year": min(yearly.items(), key=lambda kv: kv[1]), "yearly": yearly}


def run(px):
    """px: {symbol: {date: close}}. Returns curves and trade stats."""
    dates = sorted(d for d in px["SPY"] if d >= START)
    allx = {s: sorted(px[s].items()) for s in px}
    idx = {s: {d: i for i, (d, _) in enumerate(allx[s])} for s in px}

    def hist(s, d, n):
        i = idx[s].get(d)
        if i is None or i + 1 < n:
            return None
        return [c for _, c in allx[s][i + 1 - n:i + 1]]

    def ret(s, d0, d1):
        a, b = px[s].get(d0), px[s].get(d1)
        return (b / a - 1) if a and b else 0.0

    hold, filt, filt_cash = [1.0], [1.0], [1.0]
    regime = "alta"
    flips = 0
    # short-term sleeve: positions {key: {"sym","inverse","entry","value","days"}}, cash
    cash, pos, curto = 1.0, {}, [1.0]
    trades = []
    for k in range(len(dates) - 1):
        d, nd = dates[k], dates[k + 1]
        r_spy = ret("SPY", d, nd)
        # --- filtro (decide on d's close)
        h = hist("SPY", d, 200)
        if h:
            avg, p = sum(h) / 200, h[-1]
            new = regime
            if regime == "alta" and p < avg * 0.98:
                new = "queda"
            elif regime == "queda" and p > avg:
                new = "alta"
            if new != regime:
                flips += 1
                filt[-1] *= (1 - COST) ** 2
                filt_cash[-1] *= (1 - COST)
                regime = new
        hold.append(hold[-1] * (1 + r_spy))
        if regime == "alta":
            filt.append(filt[-1] * (1 + r_spy))
            filt_cash.append(filt_cash[-1] * (1 + r_spy))
        else:
            filt.append(filt[-1] * (1 - r_spy - SH_FEE))
            filt_cash.append(filt_cash[-1])

        # --- curto: exits on d's close
        for key in list(pos):
            p_ = pos[key]
            s = p_["sym"]
            h5 = hist(s, d, 5)
            if not h5:
                continue
            last = h5[-1]
            sma5 = sum(h5) / 5
            # the position's own price path: entry value vs now
            gain = p_["value"] / p_["cost"] - 1
            exit_ = (gain <= -0.08 or p_["days"] >= 10 or
                     (not p_["inverse"] and last > sma5) or (p_["inverse"] and last < sma5))
            if exit_:
                cash += p_["value"] * (1 - COST)
                trades.append((p_["value"] * (1 - COST) / p_["cost"] - 1, p_["inverse"]))
                del pos[key]
        # entries on d's close
        value = cash + sum(p_["value"] for p_ in pos.values())
        slot = value / 4
        cands = []
        for s in ETFS:
            if s not in px:
                continue
            h200 = hist(s, d, 200)
            if not h200:
                continue
            p, avg = h200[-1], sum(h200) / 200
            r = rsi2(h200[-60:])
            if r is None:
                continue
            if r < 10 and p > avg and s not in pos:
                cands.append((r, s, False))
            if s in INVERSE_OF and r > 90 and p < avg and ("inv-" + s) not in pos:
                cands.append((100 - r, s, True))
        for _, s, inv in sorted(cands)[:max(0, 4 - len(pos))]:
            amt = min(slot, cash)
            if amt < value * 0.01:
                break
            cash -= amt
            key = ("inv-" if inv else "") + s
            pos[key] = {"sym": s, "inverse": inv, "cost": amt, "value": amt * (1 - COST), "days": 0}
        # next day's move
        for p_ in pos.values():
            r = ret(p_["sym"], d, nd)
            p_["value"] *= (1 + (-r - SH_FEE if p_["inverse"] else r))
            p_["days"] += 1
        curto.append(cash + sum(p_["value"] for p_ in pos.values()))

    def mix(x, y, wx=0.8):
        """wx of x and the rest of y, rebalanced each January."""
        out, a, b = [1.0], wx, 1 - wx
        for k in range(1, len(dates)):
            a *= x[k] / x[k - 1]
            b *= y[k] / y[k - 1]
            if dates[k][:4] != dates[k - 1][:4]:
                a, b = (a + b) * wx, (a + b) * (1 - wx)
            out.append(a + b)
        return out

    def tstats(ts):
        return {"n": len(ts), "win_rate": sum(1 for t in ts if t > 0) / len(ts) if ts else 0,
                "avg": sum(ts) / len(ts) if ts else 0}

    longs = [t for t, inv in trades if not inv]
    shorts = [t for t, inv in trades if inv]
    wins = [t for t, _ in trades if t > 0]
    return dates, {"hold": hold, "filtro": filt, "filtro_caixa": filt_cash, "curto": curto,
                   "robo": mix(filt, curto), "hold_curto": mix(hold, curto),
                   "caixa_curto": mix(filt_cash, curto)}, {
        "flips": flips, "trades": len(trades), "win_rate": len(wins) / len(trades) if trades else 0,
        "avg_trade": sum(t for t, _ in trades) / len(trades) if trades else 0,
        "long": tstats(longs), "inverse": tstats(shorts),
        "invested_share": None}


LABELS = {"hold": "Só SPY (comprar e segurar)", "filtro": "Filtro de mercado (SPY ↔ SH)",
          "filtro_caixa": "Filtro de mercado (SPY ↔ caixa)", "curto": "Curto prazo (RSI 2)",
          "robo": "Robô hoje (80% filtro SH + 20% curto)",
          "hold_curto": "80% SPY + 20% curto", "caixa_curto": "80% filtro caixa + 20% curto"}


def report(dates, curves, tr):
    rows = {k: stats(v, dates) for k, v in curves.items()}

    def pct(x):
        return "%+.1f%%" % (100 * x)

    out = ["# Teste no passado (%s a %s)" % (dates[0], dates[-1]), "",
           "US$ 10.000 aplicados no início, com custo de 0,05% por operação. O SPY entra no lugar das ações "
           "do longo prazo (o screener não pode ser testado no passado).", "",
           "| Estratégia | Rende por ano | Total | Pior queda | Pior ano | US$ 10 mil viraram |",
           "| --- | --- | --- | --- | --- | --- |"]
    for k in ("hold", "filtro", "filtro_caixa", "curto", "robo", "hold_curto", "caixa_curto"):
        s = rows[k]
        out.append("| %s | %s | %s | %s | %s (%s) | $%s |" % (
            LABELS[k], pct(s["cagr"]), pct(s["total"]), pct(s["max_drawdown"]), s["worst_year"][0],
            pct(s["worst_year"][1]), "{:,.0f}".format(10000 * (1 + s["total"]))))
    out += ["", "Curto prazo: %d operações, %.0f%% com lucro, média de %s por operação. Filtro de mercado: "
            "%d trocas de lado." % (tr["trades"], 100 * tr["win_rate"], pct(tr["avg_trade"]), tr["flips"]), "",
            "- Compras na queda (a favor do mercado): %d operações, %.0f%% com lucro, média %s."
            % (tr["long"]["n"], 100 * tr["long"]["win_rate"], pct(tr["long"]["avg"])),
            "- Apostas na queda com ETF inverso (SH, PSQ, DOG, RWM): %d operações, %.0f%% com lucro, média %s."
            % (tr["inverse"]["n"], 100 * tr["inverse"]["win_rate"], pct(tr["inverse"]["avg"])), "",
            "## Ano a ano", "", "| Ano | " + " | ".join(LABELS[k] for k in ("hold", "filtro", "curto", "robo")) + " |",
            "| --- | --- | --- | --- | --- |"]
    for y in sorted(rows["hold"]["yearly"]):
        out.append("| %s | %s |" % (y, " | ".join(pct(rows[k]["yearly"][y]) for k in ("hold", "filtro", "curto", "robo"))))
    out += ["", "_Resultado passado não garante resultado futuro. Não é recomendação de investimento._"]
    return "\n".join(out) + "\n", rows


def main():
    px = {}
    for s in ETFS:
        try:
            px[s] = yahoo(s)
            print("%s: %d dias" % (s, len(px[s])))
        except Exception as e:
            print("aviso: sem histórico de %s (%s)" % (s, e), file=sys.stderr)
    if "SPY" not in px:
        print("erro: sem histórico do SPY", file=sys.stderr)
        return 1
    dates, curves, tr = run(px)
    md, rows = report(dates, curves, tr)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "resultado.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(os.path.join(OUT, "resultado.json"), "w", encoding="utf-8") as fh:
        json.dump({"dates": [dates[0], dates[-1]], "trades": tr,
                   "summary": {k: {kk: vv for kk, vv in v.items() if kk != "yearly"} for k, v in rows.items()},
                   "yearly": {k: v["yearly"] for k, v in rows.items()}}, fh, indent=2)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
