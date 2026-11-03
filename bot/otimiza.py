#!/usr/bin/env python3
"""Parameter search for the short-term sleeve, guarded against overfitting.

Every combination of the sleeve's numbers is simulated on 2007-2016 (training) and, separately,
on 2017 onwards (out of sample). A setting is only worth adopting if it also holds up on the years
it was not chosen on. Same mechanics as bot/backtest.py (decide on a close, earn the next day's
return, 0.05% per trade), with indicators precomputed once so a few hundred runs take seconds.

    python3 bot/otimiza.py          # downloads history (needs network), writes trader/backtest/otimizacao.md
"""

import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402

TRAIN = ("2007-01-01", "2016-12-31")
TEST = ("2017-01-01", "2099-12-31")
CURRENT = {"rsi_entry": 10, "rsi_short": 90, "stop": 0.08, "max_days": 10, "exit_sma": 5}
GRID = {"rsi_entry": [5, 10, 15, 20], "rsi_short": [80, 85, 90, 95], "stop": [0.05, 0.08, 0.12, 1.0],
        "max_days": [5, 10, 15], "exit_sma": [3, 5]}


def indicators(px):
    """Per symbol: dates, closes, 200-day average, 3- and 5-day averages, Wilder RSI(2), by index."""
    out = {}
    for s, series in px.items():
        dates = sorted(series)
        c = [series[d] for d in dates]
        n = len(c)
        sma = {k: [None] * n for k in (3, 5, 200)}
        for k in sma:
            run = 0.0
            for i in range(n):
                run += c[i]
                if i >= k:
                    run -= c[i - k]
                if i >= k - 1:
                    sma[k][i] = run / k
        rsi = [None] * n
        gain = loss = 0.0
        for i in range(1, n):
            d = c[i] - c[i - 1]
            g, l_ = max(d, 0), max(-d, 0)
            if i <= 2:
                gain += g / 2
                loss += l_ / 2
                if i == 2:
                    rsi[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
                continue
            gain = (gain + g) / 2
            loss = (loss + l_) / 2
            rsi[i] = 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
        out[s] = {"idx": {d: i for i, d in enumerate(dates)}, "c": c, "sma": sma, "rsi": rsi}
    return out


def simulate(ind, px, dates, p):
    cash, pos, curve, trades = 1.0, {}, [1.0], []
    inverse_of = set(bt.INVERSE_OF)
    for k in range(len(dates) - 1):
        d, nd = dates[k], dates[k + 1]
        for key in list(pos):
            q = pos[key]
            x = ind[q["sym"]]
            i = x["idx"].get(d)
            if i is None or x["sma"][p["exit_sma"]][i] is None:
                continue
            last, ma = x["c"][i], x["sma"][p["exit_sma"]][i]
            gain = q["value"] / q["cost"] - 1
            if (gain <= -p["stop"] or q["days"] >= p["max_days"] or
                    (not q["inv"] and last > ma) or (q["inv"] and last < ma)):
                cash += q["value"] * (1 - bt.COST)
                trades.append(q["value"] * (1 - bt.COST) / q["cost"] - 1)
                del pos[key]
        value = cash + sum(q["value"] for q in pos.values())
        cands = []
        for s in ind:
            x = ind[s]
            i = x["idx"].get(d)
            if i is None or x["sma"][200][i] is None or x["rsi"][i] is None:
                continue
            last, avg, r = x["c"][i], x["sma"][200][i], x["rsi"][i]
            if r < p["rsi_entry"] and last > avg and s not in pos:
                cands.append((r, s, False))
            if s in inverse_of and r > p["rsi_short"] and last < avg and ("inv-" + s) not in pos:
                cands.append((100 - r, s, True))
        for _, s, inv in sorted(cands)[:max(0, 4 - len(pos))]:
            amt = min(value / 4, cash)
            if amt < value * 0.01:
                break
            cash -= amt
            pos[("inv-" if inv else "") + s] = {"sym": s, "inv": inv, "cost": amt, "value": amt * (1 - bt.COST),
                                                "days": 0}
        for q in pos.values():
            a, b = px[q["sym"]].get(d), px[q["sym"]].get(nd)
            r = (b / a - 1) if a and b else 0.0
            q["value"] *= 1 + (-r - bt.SH_FEE if q["inv"] else r)
            q["days"] += 1
        curve.append(cash + sum(q["value"] for q in pos.values()))
    st = bt.stats(curve, dates)
    st["trades"] = len(trades)
    st["win"] = sum(1 for t in trades if t > 0) / len(trades) if trades else 0
    return st


def window(px, lo, hi):
    return sorted(d for d in px["SPY"] if lo <= d <= hi)


def label(p):
    return "RSI<%d / RSI>%d / stop %s / %d dias / média %d" % (
        p["rsi_entry"], p["rsi_short"], "sem" if p["stop"] >= 1 else "%d%%" % round(100 * p["stop"]),
        p["max_days"], p["exit_sma"])


def main():
    px = {}
    for s in bt.ETFS:
        try:
            px[s] = bt.yahoo(s)
        except Exception as e:
            print("aviso: sem histórico de %s (%s)" % (s, e), file=sys.stderr)
    if "SPY" not in px:
        print("erro: sem histórico do SPY", file=sys.stderr)
        return 1
    ind = indicators(px)
    tr_dates, te_dates = window(px, *TRAIN), window(px, *TEST)
    keys = list(GRID)
    results = []
    for combo in itertools.product(*(GRID[k] for k in keys)):
        p = dict(zip(keys, combo))
        a, b = simulate(ind, px, tr_dates, p), simulate(ind, px, te_dates, p)
        results.append({"p": p, "train": a, "test": b, "current": p == CURRENT})

    def calmar(s):
        return s["cagr"] / abs(s["max_drawdown"]) if s["max_drawdown"] else 0

    by_train = sorted(results, key=lambda r: -calmar(r["train"]))
    cur = next(r for r in results if r["current"])
    # robust: good in both halves = best by the worse of the two Calmar ratios
    robust = sorted(results, key=lambda r: -min(calmar(r["train"]), calmar(r["test"])))

    def pct(x):
        return "%+.1f%%" % (100 * x)

    def row(r):
        a, b = r["train"], r["test"]
        return "| %s%s | %s | %s | %s | %s | %s | %d |" % (
            label(r["p"]), " **(atual)**" if r["current"] else "", pct(a["cagr"]), pct(a["max_drawdown"]),
            pct(b["cagr"]), pct(b["max_drawdown"]), "%.0f%%" % (100 * b["win"]), b["trades"])

    head = ["| Regras (entrada / aposta na queda / stop / prazo / média de saída) | Treino: ao ano | Treino: pior queda "
            "| Prova: ao ano | Prova: pior queda | Prova: acertos | Prova: operações |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    out = ["# Otimização do curto prazo", "",
           "%d combinações testadas. **Treino** %s a %s (onde as regras são escolhidas) e **prova** %s a %s "
           "(anos que a escolha não viu). Nota = retorno ao ano dividido pela pior queda." % (
               len(results), tr_dates[0], tr_dates[-1], te_dates[0], te_dates[-1]), "",
           "## Regras atuais", ""] + head + [row(cur), "",
           "## As 10 mais robustas (boas no treino e na prova)", ""] + head + [row(r) for r in robust[:10]] + [
           "", "## As 10 melhores só no treino (cuidado: podem ter \"decorado\" o passado)", ""] + head + [
           row(r) for r in by_train[:10]] + [
           "", "_Resultado passado não garante resultado futuro. Não é recomendação de investimento._"]
    os.makedirs(bt.OUT, exist_ok=True)
    with open(os.path.join(bt.OUT, "otimizacao.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    with open(os.path.join(bt.OUT, "otimizacao.json"), "w", encoding="utf-8") as fh:
        slim = lambda rs: [{"p": r["p"], "train": {k: v for k, v in r["train"].items() if k != "yearly"},  # noqa: E731
                            "test": {k: v for k, v in r["test"].items() if k != "yearly"}} for r in rs]
        json.dump({"current": slim([cur])[0], "robust": slim(robust[:10]), "by_train": slim(by_train[:10])}, fh,
                  indent=2, default=str)
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
