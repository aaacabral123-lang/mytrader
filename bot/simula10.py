#!/usr/bin/env python3
"""What US$ 5,000 would have become over the last 10 years with the robot's possible allocations.

Daily simulation from 10 years ago to today (dividend-adjusted closes, 0.05% per trade, decide on a
close and earn the next day's return), each allocation rebalanced every January:

    SPY              buy and hold SPY (the index sleeve's VOO tracks the same index)
    atual            50% index + 30% pairs + 20% short-term (the robot today)
    plano            80% pairs + 20% short-term (the plan for Friday's review)
    pares            100% pairs

Pairs use the live settings (bot/pares_robo.py); short-term uses the live RSI(2) rules (bot/backtest.py).
Writes trader/backtest/simulacao_10anos.md.

    python3 bot/simula10.py
"""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402
import pares  # noqa: E402
import pares_robo  # noqa: E402

START_MONEY = 5000.0


def mix(curves, weights, dates):
    parts = {k: w for k, w in weights.items()}
    out = [1.0]
    for i in range(1, len(dates)):
        for k in parts:
            parts[k] *= curves[k][i] / curves[k][i - 1]
        if dates[i][:4] != dates[i - 1][:4]:
            tot = sum(parts.values())
            parts = {k: tot * w for k, w in weights.items()}
        out.append(sum(parts.values()))
    return out


def main():
    today = dt.date.today()
    start = today.replace(year=today.year - 10).isoformat()
    cfg = pares_robo.PARES_DEFAULTS
    pairs = [tuple(p) for p in cfg["pairs"]]
    syms = sorted({s for p in pairs for s in p} | set(bt.ETFS))
    px = {}
    for s in syms:
        try:
            px[s] = bt.yahoo(s)
        except Exception as e:
            print("aviso: sem histórico de %s (%s)" % (s, e), file=sys.stderr)
    bt.START = start
    dates, curves, _ = bt.run({s: px[s] for s in bt.ETFS if s in px})
    pairs = [p for p in pairs if p[0] in px and p[1] in px]
    pre = pares.precompute(px, pairs, [cfg["lookback"]])
    pst = pares.simulate(px, pre, dates, {"lookback": cfg["lookback"], "z_in": cfg["z_in"], "z_out": cfg["z_out"],
                                          "min_corr": cfg["min_corr"], "max_days": cfg["max_days"], "stop": 1.0})
    base = {"spy": curves["hold"], "curto": curves["curto"], "pares": pst["curve"]}
    alloc = {
        "SPY (só o índice)": {"spy": 1.0},
        "Atual: 50% índice + 30% pares + 20% curto": {"spy": 0.5, "pares": 0.3, "curto": 0.2},
        "Plano: 80% pares + 20% curto": {"pares": 0.8, "curto": 0.2},
        "100% pares": {"pares": 1.0},
    }
    res = {name: mix(base, w, dates) for name, w in alloc.items()}
    stats = {name: bt.stats(c, dates) for name, c in res.items()}

    def pct(x):
        return "%+.1f%%" % (100 * x)

    out = ["# Simulação dos últimos 10 anos (%s a %s)" % (dates[0], dates[-1]), "",
           "US$ 5.000 aplicados no início, rebalanceado todo janeiro, custo de 0,05% por operação. "
           "Pares com as regras que o robô usa hoje; curto prazo com as regras de RSI(2).", "",
           "| Carteira | US$ 5.000 viraram | Rende por ano | Total | Pior queda | Pior ano |",
           "| --- | --- | --- | --- | --- | --- |"]
    for name, s in stats.items():
        out.append("| %s | $%s | %s | %s | %s | %s (%s) |" % (
            name, "{:,.0f}".format(START_MONEY * (1 + s["total"])), pct(s["cagr"]), pct(s["total"]),
            pct(s["max_drawdown"]), s["worst_year"][0], pct(s["worst_year"][1])))
    out += ["", "Pares sozinhos: %d operações, %.0f%% com lucro, média de %s por operação." % (
        pst["trades"], 100 * pst["win"], pct(pst["avg"])), "",
        "## Ano a ano", "", "| Ano | " + " | ".join(stats) + " |", "| --- |" + " --- |" * len(stats)]
    for y in sorted(stats["SPY (só o índice)"]["yearly"]):
        out.append("| %s | %s |" % (y, " | ".join(pct(stats[n]["yearly"][y]) for n in stats)))
    out += ["", "_Resultado passado não garante resultado futuro. Os pares foram escolhidos hoje, entre empresas "
            "que sobreviveram e cresceram, o que deixa o passado um pouco mais bonito do que seria em tempo real. "
            "Não é recomendação de investimento._"]
    os.makedirs(bt.OUT, exist_ok=True)
    with open(os.path.join(bt.OUT, "simulacao_10anos.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
