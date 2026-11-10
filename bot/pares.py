#!/usr/bin/env python3
"""Backtest of pairs trading, long only: buy the laggard of two stocks that usually move together.

For each pair (A, B) the "spread" is log(A) - log(B). Its z-score over `lookback` days says how far
apart the two have drifted compared with normal. When the z-score passes `z_in` (one fell behind
the other) and the pair has actually been moving together lately (correlation of daily returns over
120 days above `min_corr`), buy the one that fell behind. Sell when the z-score comes back inside
`z_out`, at a `stop` loss, or after `max_days`. Up to `slots` trades at a time, equal size.

Same mechanics as bot/backtest.py: decide on a day's close, earn the next day's return, 0.05% per
trade, dividend-adjusted closes. Every setting is run on 2007-2016 (training) and 2017 onwards
(out of sample). Results go to trader/backtest/pares.md.

    python3 bot/pares.py            # downloads history (needs network)
"""

import itertools
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402

PAIRS = [("KO", "PEP"), ("V", "MA"), ("HD", "LOW"), ("XOM", "CVX"), ("JPM", "BAC"), ("GS", "MS"),
         ("UPS", "FDX"), ("MCD", "YUM"), ("WMT", "TGT"), ("PG", "CL"), ("CAT", "DE"), ("T", "VZ"),
         ("UNP", "CSX"), ("LMT", "NOC"), ("PFE", "MRK"), ("COST", "WMT"),
         ("MSFT", "AAPL"), ("C", "WFC"), ("COP", "XOM")]
TRAIN = ("2007-01-01", "2016-12-31")
TEST = ("2017-01-01", "2099-12-31")
GRID = {"lookback": [20, 60], "z_in": [1.5, 2.0, 2.5], "z_out": [0.0, 0.5], "min_corr": [0.5, 0.7],
        "max_days": [10, 20], "stop": [0.10, 1.0]}
SLOTS = 5


def prepare(px, a, b):
    """Aligned dates and, per date: log spread, rolling mean/std pieces, 120-day return correlation."""
    dates = sorted(set(px[a]) & set(px[b]))
    la = [math.log(px[a][d]) for d in dates]
    lb = [math.log(px[b][d]) for d in dates]
    spread = [x - y for x, y in zip(la, lb)]
    ra = [0.0] + [la[i] - la[i - 1] for i in range(1, len(la))]
    rb = [0.0] + [lb[i] - lb[i - 1] for i in range(1, len(lb))]
    return {"dates": dates, "idx": {d: i for i, d in enumerate(dates)}, "spread": spread, "ra": ra, "rb": rb}


def zscore(p, i, n):
    if i + 1 < n:
        return None
    w = p["spread"][i + 1 - n:i + 1]
    m = sum(w) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return (w[-1] - m) / sd if sd > 0 else None


def corr(p, i, n=120):
    if i + 1 < n:
        return None
    a, b = p["ra"][i + 1 - n:i + 1], p["rb"][i + 1 - n:i + 1]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va, vb = sum((x - ma) ** 2 for x in a), sum((y - mb) ** 2 for y in b)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else None


def precompute(px, pairs, lookbacks):
    """z-scores and correlations per pair, per date, per lookback: computed once for all settings."""
    out = {}
    for a, b in pairs:
        p = prepare(px, a, b)
        z = {n: {} for n in lookbacks}
        c = {}
        for i, d in enumerate(p["dates"]):
            for n in lookbacks:
                z[n][d] = zscore(p, i, n)
            c[d] = corr(p, i)
        out[(a, b)] = {"z": z, "corr": c}
    return out


def simulate(px, pre, dates, s):
    cash, pos, curve, trades = 1.0, {}, [1.0], []
    for k in range(len(dates) - 1):
        d, nd = dates[k], dates[k + 1]
        for key in list(pos):
            q = pos[key]
            z = pre[q["pair"]]["z"][s["lookback"]].get(d)
            gain = q["value"] / q["cost"] - 1
            back = z is not None and (z <= s["z_out"] if q["sym"] == q["pair"][1] else z >= -s["z_out"])
            if back or gain <= -s["stop"] or q["days"] >= s["max_days"]:
                cash += q["value"] * (1 - bt.COST)
                trades.append(q["value"] * (1 - bt.COST) / q["cost"] - 1)
                del pos[key]
        value = cash + sum(q["value"] for q in pos.values())
        cands = []
        for pair, x in pre.items():
            if pair in [q["pair"] for q in pos.values()]:
                continue
            z, c = x["z"][s["lookback"]].get(d), x["corr"].get(d)
            if z is None or c is None or c < s["min_corr"]:
                continue
            # z = log(A) - log(B) high: A ran ahead, B fell behind -> buy B; z low -> buy A
            if z >= s["z_in"]:
                cands.append((-abs(z), pair, pair[1]))
            elif z <= -s["z_in"]:
                cands.append((-abs(z), pair, pair[0]))
        for _, pair, sym in sorted(cands)[:max(0, SLOTS - len(pos))]:
            amt = min(value / SLOTS, cash)
            if amt < value * 0.01:
                break
            cash -= amt
            pos[pair] = {"pair": pair, "sym": sym, "cost": amt, "value": amt * (1 - bt.COST), "days": 0}
        for q in pos.values():
            a, b = px[q["sym"]].get(d), px[q["sym"]].get(nd)
            q["value"] *= 1 + ((b / a - 1) if a and b else 0.0)
            q["days"] += 1
        curve.append(cash + sum(q["value"] for q in pos.values()))
    st = bt.stats(curve, dates)
    st["trades"] = len(trades)
    st["win"] = sum(1 for t in trades if t > 0) / len(trades) if trades else 0
    st["avg"] = sum(trades) / len(trades) if trades else 0
    return st


def basket(px, syms, dates):
    """Control: hold the same stocks in equal parts, rebalanced each January (no pair signal at all)."""
    hold = [s for s in syms if s in px]
    curve, w = [1.0], {}
    for k in range(len(dates) - 1):
        d, nd = dates[k], dates[k + 1]
        if k == 0 or dates[k][:4] != dates[k - 1][:4]:
            live = [s for s in hold if px[s].get(d)]
            w = {s: curve[-1] / len(live) for s in live}
        for s in w:
            a, b = px[s].get(d), px[s].get(nd)
            w[s] *= 1 + ((b / a - 1) if a and b else 0.0)
        curve.append(sum(w.values()))
    return bt.stats(curve, dates)


def main():
    syms = sorted({s for p in PAIRS for s in p} | {"SPY"})
    px = {}
    for s in syms:
        try:
            px[s] = bt.yahoo(s)
        except Exception as e:
            print("aviso: sem histórico de %s (%s)" % (s, e), file=sys.stderr)
    pairs = [p for p in PAIRS if p[0] in px and p[1] in px]
    pre = precompute(px, pairs, GRID["lookback"])
    spy = {}
    windows = {}
    for name, (lo, hi) in (("train", TRAIN), ("test", TEST)):
        windows[name] = sorted(d for d in px["SPY"] if lo <= d <= hi)
        c = [px["SPY"][d] for d in windows[name]]
        spy[name] = bt.stats([v / c[0] for v in c], windows[name])
    stocks = sorted({s for p in pairs for s in p})
    ctrl = {name: basket(px, stocks, windows[name]) for name in windows}
    keys = list(GRID)
    rows = []
    for combo in itertools.product(*(GRID[k] for k in keys)):
        s = dict(zip(keys, combo))
        rows.append({"s": s, "train": simulate(px, pre, windows["train"], s),
                     "test": simulate(px, pre, windows["test"], s)})

    def calmar(x):
        return x["cagr"] / abs(x["max_drawdown"]) if x["max_drawdown"] else 0

    robust = sorted(rows, key=lambda r: -min(calmar(r["train"]), calmar(r["test"])))
    by_train = sorted(rows, key=lambda r: -calmar(r["train"]))

    def pct(x):
        return "%+.1f%%" % (100 * x)

    def label(s):
        return "z %.1f→%.1f / %d dias de base / correlação ≥ %.1f / até %d dias / stop %s" % (
            s["z_in"], s["z_out"], s["lookback"], s["min_corr"], s["max_days"],
            "sem" if s["stop"] >= 1 else "%d%%" % round(100 * s["stop"]))

    def row(r):
        a, b = r["train"], r["test"]
        return "| %s | %s | %s | %s | %s | %.0f%% | %d | %s |" % (
            label(r["s"]), pct(a["cagr"]), pct(a["max_drawdown"]), pct(b["cagr"]), pct(b["max_drawdown"]),
            100 * b["win"], b["trades"], pct(b["avg"]))

    head = ["| Regra | Treino: ao ano | Treino: pior queda | Prova: ao ano | Prova: pior queda | Prova: acertos "
            "| Prova: operações | Prova: média por operação |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    out = ["# Teste: pares de empresas correlacionadas (comprar a que ficou para trás)", "",
           "%d pares: %s. %d versões da regra. **Treino** %s a %s, **prova** %s a %s. Até %d operações ao "
           "mesmo tempo, mesmo tamanho; custo de 0,05%% por compra ou venda." % (
               len(pairs), ", ".join("%s/%s" % p for p in pairs), len(rows), windows["train"][0],
               windows["train"][-1], windows["test"][0], windows["test"][-1], SLOTS), "",
           "**Para comparar, só SPY:** treino %s ao ano (pior queda %s), prova %s ao ano (pior queda %s)." % (
               pct(spy["train"]["cagr"]), pct(spy["train"]["max_drawdown"]), pct(spy["test"]["cagr"]),
               pct(spy["test"]["max_drawdown"])),
           "**Controle, as mesmas %d empresas compradas e seguradas em partes iguais (sem a regra dos pares):** "
           "treino %s ao ano (pior queda %s), prova %s ao ano (pior queda %s). A regra só agrega valor se "
           "render mais do que isso." % (len(stocks), pct(ctrl["train"]["cagr"]), pct(ctrl["train"]["max_drawdown"]),
                                          pct(ctrl["test"]["cagr"]), pct(ctrl["test"]["max_drawdown"])), "",
           "## As 10 versões mais robustas (boas no treino e na prova)", ""] + head + [row(r) for r in robust[:10]] + [
           "", "## As 10 melhores só no treino", ""] + head + [row(r) for r in by_train[:10]] + [
           "", "_Resultado passado não garante resultado futuro. Não é recomendação de investimento._"]
    os.makedirs(bt.OUT, exist_ok=True)
    with open(os.path.join(bt.OUT, "pares.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
