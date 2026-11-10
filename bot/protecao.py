#!/usr/bin/env python3
"""Backtest: run the pairs strategy normally and switch to a protection asset only while the market falls.

Signal (as in bot/backup.py): SPY closes `buffer`% under its N-day average -> falling; back above the
average -> rising. While rising, the money follows the pairs sleeve (live settings). While falling,
it sits in one protection asset: DBMF or KMLM (managed-futures trend ETFs), GLD (gold), TLT (long
Treasuries), SH (-1x S&P 500) or cash. Compared with staying in the pairs all the time.

The managed-futures ETFs only exist since 2019/2020, so the test starts when the chosen asset has
history (each row says from when). 0.05% per trade and per switch.

    python3 bot/protecao.py      # writes trader/backtest/protecao.md
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as bt  # noqa: E402
import pares  # noqa: E402
import pares_robo  # noqa: E402

HEDGES = ["DBMF", "KMLM", "GLD", "TLT", "SH", "caixa"]


def regimes(spy, dates, n=50, buffer=0.02):
    hist = sorted(spy)
    idx = {d: i for i, d in enumerate(hist)}
    closes = [spy[d] for d in hist]
    out, reg = {}, "alta"
    for d in dates:
        i = idx[d]
        if i + 1 >= n:
            avg = sum(closes[i + 1 - n:i + 1]) / n
            p = closes[i]
            if reg == "alta" and p < avg * (1 - buffer):
                reg = "queda"
            elif reg == "queda" and p > avg:
                reg = "alta"
        out[d] = reg
    return out


def main():
    cfg = pares_robo.PARES_DEFAULTS
    pairs = [tuple(p) for p in cfg["pairs"]]
    syms = sorted({s for p in pairs for s in p} | {"SPY"} | {h for h in HEDGES if h != "caixa"})
    px = {}
    for s in syms:
        try:
            px[s] = bt.yahoo(s)
        except Exception as e:
            print("aviso: sem histórico de %s (%s)" % (s, e), file=sys.stderr)
    pairs = [p for p in pairs if p[0] in px and p[1] in px]
    pre = pares.precompute(px, pairs, [cfg["lookback"]])
    settings = {"lookback": cfg["lookback"], "z_in": cfg["z_in"], "z_out": cfg["z_out"],
                "min_corr": cfg["min_corr"], "max_days": cfg["max_days"], "stop": 1.0}

    def pct(x):
        return "%+.1f%%" % (100 * x)

    out = ["# Proteção só na queda: pares normalmente, ativo de proteção enquanto o mercado cai", "",
           "Sinal: SPY fecha 2% abaixo da média de 50 dias = queda; volta acima da média = alta. Na alta, "
           "estratégia dos pares (regras atuais). Na queda, vende os pares e fica no ativo de proteção. "
           "Custo de 0,05% por operação e por troca.", "",
           "| Proteção na queda | Período | Só pares: ao ano | Com a proteção: ao ano | Só pares: pior queda "
           "| Com a proteção: pior queda | Trocas |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for h in HEDGES:
        first = min(px[h]) if h in px else "2007-01-03"
        start = max(first, "2007-01-03")
        # skip the first 60 days of a young ETF so the comparison starts on a normal day
        dates = sorted(d for d in px["SPY"] if d >= start)[60:]
        reg = regimes(px["SPY"], dates)
        ps = pares.simulate(px, pre, dates, settings)
        curve, switches, prev = [1.0], 0, "alta"
        for k in range(len(dates) - 1):
            d, nd = dates[k], dates[k + 1]
            if reg[d] != prev:
                switches += 1
                curve[-1] *= (1 - bt.COST) ** 2
                prev = reg[d]
            if reg[d] == "alta":
                r = ps["curve"][k + 1] / ps["curve"][k] - 1
            elif h == "caixa":
                r = 0.0
            else:
                a, b = px[h].get(d), px[h].get(nd)
                r = (b / a - 1) if a and b else 0.0
            curve.append(curve[-1] * (1 + r))
        st = bt.stats(curve, dates)
        out.append("| %s | %s a %s | %s | %s | %s | %s | %d |" % (
            h, dates[0][:7], dates[-1][:7], pct(ps["cagr"]), pct(st["cagr"]), pct(ps["max_drawdown"]),
            pct(st["max_drawdown"]), switches))
    out += ["", "_Resultado passado não garante resultado futuro. Não é recomendação de investimento._"]
    os.makedirs(bt.OUT, exist_ok=True)
    with open(os.path.join(bt.OUT, "protecao.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
