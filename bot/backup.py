#!/usr/bin/env python3
"""Backtest of a "backup plan" for falling markets: leave the market for an inverse ETF and come back.

Signal: SPY closes `buffer`% under its N-day average -> "falling"; back above the average -> "rising".
While rising, hold SPY. While falling, hold one of:
    caixa   cash
    SH      -1x SPY each day (minus its 0.89% a year fee)
    SQQQ    -3x QQQ each day (minus its 0.95% a year fee): the daily reset makes it lose value in
            choppy markets even when QQQ ends flat
Compared with holding SPY. Same mechanics as bot/backtest.py: decide on a close, earn the next
day's return, 0.05% per switch. Full period 2007 onwards and the last 10 years.

    python3 bot/backup.py      # writes trader/backtest/backup_queda.md
"""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402

FEES = {"SH": 0.0089 / 252, "SQQQ": 0.0095 / 252}


def simulate(px, dates, n, buffer, hedge):
    spy, qqq = px["SPY"], px["QQQ"]
    hist = sorted(spy)
    idx = {d: i for i, d in enumerate(hist)}
    closes = [spy[d] for d in hist]
    curve, regime, switches, days_down = [1.0], "alta", 0, 0
    for k in range(len(dates) - 1):
        d, nd = dates[k], dates[k + 1]
        i = idx[d]
        if i + 1 >= n:
            avg = sum(closes[i + 1 - n:i + 1]) / n
            p = closes[i]
            new = "queda" if regime == "alta" and p < avg * (1 - buffer) else \
                  "alta" if regime == "queda" and p > avg else regime
            if new != regime:
                switches += 1
                curve[-1] *= (1 - bt.COST) ** 2
                regime = new
        rs = spy[nd] / spy[d] - 1
        if regime == "alta":
            r = rs
        else:
            days_down += 1
            rq = (qqq[nd] / qqq[d] - 1) if qqq.get(d) and qqq.get(nd) else 0.0
            r = {"caixa": 0.0, "SH": -rs - FEES["SH"], "SQQQ": -3 * rq - FEES["SQQQ"]}[hedge]
        curve.append(curve[-1] * (1 + r))
    st = bt.stats(curve, dates)
    st["switches"], st["share_down"] = switches, days_down / max(1, len(dates) - 1)
    return st


def main():
    px = {s: bt.yahoo(s) for s in ("SPY", "QQQ")}
    today = dt.date.today()
    periods = {"2007 até hoje": "2007-01-01", "últimos 10 anos": today.replace(year=today.year - 10).isoformat()}

    def pct(x):
        return "%+.1f%%" % (100 * x)

    out = ["# Plano B para quedas: sair do mercado para um ETF inverso e voltar", "",
           "Sinal: o SPY fecha 2% abaixo da média de N dias = queda; volta acima da média = alta. Na alta, fica "
           "no SPY. Na queda, fica em caixa, no SH (-1x S&P 500) ou no SQQQ (-3x Nasdaq). Custo de 0,05% por "
           "troca.", ""]
    for name, start in periods.items():
        dates = sorted(d for d in px["SPY"] if d >= start and d in px["QQQ"])
        hold = [px["SPY"][d] / px["SPY"][dates[0]] for d in dates]
        h = bt.stats(hold, dates)
        out += ["## %s (%s a %s)" % (name, dates[0], dates[-1]), "",
                "| Estratégia | Rende por ano | Pior queda | Pior ano | Trocas | Tempo em \"queda\" |",
                "| --- | --- | --- | --- | --- | --- |",
                "| Só SPY | %s | %s | %s (%s) | 0 | 0%% |" % (pct(h["cagr"]), pct(h["max_drawdown"]),
                                                       h["worst_year"][0], pct(h["worst_year"][1]))]
        for n in (50, 100, 200):
            for hedge in ("caixa", "SH", "SQQQ"):
                s = simulate(px, dates, n, 0.02, hedge)
                out.append("| Média de %d dias → %s | %s | %s | %s (%s) | %d | %.0f%% |" % (
                    n, hedge, pct(s["cagr"]), pct(s["max_drawdown"]), s["worst_year"][0], pct(s["worst_year"][1]),
                    s["switches"], 100 * s["share_down"]))
        out.append("")
    out.append("_Resultado passado não garante resultado futuro. Não é recomendação de investimento._")
    os.makedirs(bt.OUT, exist_ok=True)
    with open(os.path.join(bt.OUT, "backup_queda.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
