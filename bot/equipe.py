#!/usr/bin/env python3
"""The robot team: one robot per skill, each on its own cadence, run on every shift tick.

    gestor     budget, rebalance, risk, broker, short-term sleeve (bot/robo.py)   market open, every tick
    noticias   trader-news       every tick, 24 h    serious news on a held stock blocks buying it
    politicos  trader-pelosi     every 2 h, 24 h     House trades in the stocks the robot holds or follows
    vigia      trader-alerts     market open         a held stock moving 5% in a day
    analista   trader-analyst    daily, 07:00 NY     a new quarterly report asks the gestor to re-screen
    copiador   trader-copycat    daily, 08:00 NY     a new Berkshire Hathaway 13F
    contador   trader-tax        daily, 16:30 NY     gains and losses per lot (paper)

Robots hand signals to the gestor through trader/time/sinais.json (blocked symbols, a re-screen
request) and report their status to painel/time.json for the dashboard.

    python3 bot/equipe.py              # one tick: run every robot that is due
    python3 bot/equipe.py --dry-run    # the gestor simulates; the rest report as usual
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import robo  # noqa: E402  (shared helpers: skill_json, notify, read/write_json, paths)

TIME = os.path.join(ROOT, "trader", "time")
STATUS = os.path.join(ROOT, "painel", "time.json")
SIGNALS = os.path.join(TIME, "sinais.json")
BLOCK_DAYS = 5
SERIOUS_TAGS = {"legal", "risk"}
SERIOUS_8K = {"1.03", "1.05", "2.04", "2.06", "3.01", "4.02"}

ROBOTS = [
    {"id": "gestor", "nome": "Gestor", "skills": "trader-budget, trader-rebalance, trader-risk, trader-broker",
     "quando": "a cada 30 min com a bolsa aberta"},
    {"id": "noticias", "nome": "Notícias", "skills": "trader-news", "quando": "a cada 30 min, 24 h"},
    {"id": "politicos", "nome": "Políticos", "skills": "trader-pelosi", "quando": "a cada 2 h, 24 h"},
    {"id": "vigia", "nome": "Vigia de preços", "skills": "trader-alerts", "quando": "a cada 30 min com a bolsa aberta"},
    {"id": "analista", "nome": "Analista", "skills": "trader-analyst", "quando": "1x por dia, 7h de Nova York"},
    {"id": "copiador", "nome": "Copiador", "skills": "trader-copycat", "quando": "1x por dia, 8h de Nova York"},
    {"id": "contador", "nome": "Contador", "skills": "trader-tax", "quando": "1x por dia, 16h30 de Nova York"},
]


def clock():
    key = " ".join(os.environ.get("ALPACA_API_KEY", "").split())
    secret = " ".join(os.environ.get("ALPACA_SECRET_KEY", "").split())
    req = urllib.request.Request(robo.TRADE_URL + "/v2/clock",
                                 headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def watched():
    """Tickers the robot holds or follows: long-term targets and holdings, short-term holdings."""
    out = set()
    for w in (robo.read_json(robo.path("targets.json"), {}) or {}).get("weights", []):
        out.add(w["ticker"])
    for p in (robo.read_json(robo.path("portfolio.json"), {}) or {}).get("positions", []):
        out.add(p["ticker"])
    st = (robo.read_json(robo.path("state", "robo.json"), {}) or {}).get("curto") or {}
    out.update(st.get("positions", {}))
    return sorted(out)


def due(state, rid, now, ny, every_min=None, daily_at=None, need_open=False, is_open=False):
    """ny: New York local time from Alpaca's clock (it carries the right summer/winter offset)."""
    if need_open and not is_open:
        return False
    last = state.get(rid, {}).get("last_day") if daily_at else state.get(rid, {}).get("last_run")
    if daily_at:
        return ny.strftime("%H:%M") >= daily_at and last != ny.date().isoformat()
    if every_min and last:
        return (now - dt.datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds() >= every_min * 60 - 120
    return True


# ------------------------------------------------------------------ robots

def r_gestor(ctx):
    args = ["--dry-run"] if ctx["dry_run"] else []
    p = subprocess.run([sys.executable, os.path.join(ROOT, "bot", "robo.py")] + args, cwd=ROOT)
    return "rodada feita" if p.returncode == 0 else "rodada com erro (veja o diário)"


def r_noticias(ctx):
    tickers = watched()
    if not tickers:
        return "nada para vigiar ainda"
    res = robo.skill_json("trader-news/news.py", "--tickers", *(tickers + [
        "--hours", "48", "--state", "trader/state/news_time.json"]))
    if res is None:
        raise RuntimeError("trader-news não respondeu")
    serious = []
    for it in res.get("items", []):
        tags = set(it.get("tags") or [])
        items_8k = set(it.get("items") or [])
        if tags & SERIOUS_TAGS or items_8k & SERIOUS_8K:
            serious.append(it)
    sig = ctx["signals"]
    until = (ctx["today"] + dt.timedelta(days=BLOCK_DAYS)).isoformat()
    for it in serious[:5]:
        for sym in it.get("touches") or [it.get("ticker")]:
            if sym:
                sig.setdefault("bloqueio", {})[sym] = {"until": until, "why": it.get("title", "")[:140]}
        robo.notify("Notícia grave: %s" % it.get("ticker", ""),
                    "%s\nO gestor não compra %s até %s." % (it.get("title", ""), it.get("ticker", ""), until),
                    tags=["warning"], click=it.get("link"), priority=4)
    robo.write_json(os.path.join(TIME, "noticias.json"), {"as_of": ctx["now_iso"], "items": res.get("items", [])[:40]})
    return "%d novas, %d graves" % (len(res.get("items", [])), len(serious))


def r_politicos(ctx):
    tickers = watched()
    since = (ctx["today"] - dt.timedelta(days=3)).isoformat()
    pdfs = os.path.join(os.environ.get("RUNNER_TEMP") or "/tmp", "ptr-pdfs")
    res = robo.skill_json("trader-pelosi/ptr.py", "new", "--read", "--since", since, "--pdf-dir", pdfs,
                          "--state", "trader/state/pelosi_time.json", "--tickers", *tickers)
    if res is None:
        raise RuntimeError("trader-pelosi não respondeu")
    hits = []
    for rep in res.get("reports", []):
        who = rep["filing"]["name"].replace("Hon. ", "")
        for r in (rep.get("report") or {}).get("rows", []):
            if r.get("ticker") in tickers:
                hits.append("%s: %s %s, %s" % (who, robo.TXN_TYPES.get(r.get("type"), r.get("type")), r["ticker"],
                                                r.get("amount", "")))
    if hits:
        robo.notify("Políticos negociaram ações suas", "\n".join(hits[:8]), tags=["classical_building"])
    robo.write_json(os.path.join(TIME, "politicos.json"), {"as_of": ctx["now_iso"], "new": res.get("new", []),
                                                           "hits": hits})
    return "%d declarações novas, %d nas suas ações" % (len(res.get("new", [])), len(hits))


def r_vigia(ctx):
    held = [p["ticker"] for p in (robo.read_json(robo.path("portfolio.json"), {}) or {}).get("positions", [])]
    st = (robo.read_json(robo.path("state", "robo.json"), {}) or {}).get("curto") or {}
    held += list(st.get("positions", {}))
    rules = [{"id": "vigia-%s" % s, "symbol": s, "type": "pct_move", "pct": 5} for s in sorted(set(held))]
    robo.write_json(os.path.join(TIME, "vigia_regras.json"), {"rules": rules})
    if not rules:
        return "sem posições"
    res = robo.skill_json("trader-alerts/alerts.py", "check", "--rules", "trader/time/vigia_regras.json",
                          "--state", "trader/state/vigia.json", "--prices-out", "trader/state/vigia_precos.json")
    if res is None:
        raise RuntimeError("trader-alerts não respondeu")
    for ring in res.get("rings", []):
        robo.notify("Movimento forte: %s" % ring.get("symbol"), ring.get("message", ""), tags=["zap"])
    return "%d ações vigiadas, %d alertas" % (len(rules), len(res.get("rings", [])))


def r_analista(ctx, state):
    tickers = [t for t in watched() if t.isalpha()]
    if not tickers:
        return "nada para analisar ainda"
    mfile = os.path.join(TIME, "metricas.json")
    if os.path.exists(mfile):
        os.remove(mfile)
    res = robo.skill_json("trader-analyst/facts.py", *(tickers + ["--no-filings", "--metrics", "trader/time/metricas.json"]))
    companies = (robo.read_json(mfile, {}) or {}).get("companies", [])
    if res is None and not companies:
        raise RuntimeError("trader-analyst não respondeu")
    changes = {r.get("ticker"): (r.get("changes") or [""])[0] for r in (res or []) if isinstance(r, dict)}
    seen = state.setdefault("analista", {}).setdefault("periods", {})
    fresh = []
    for c in companies:
        t, per = c.get("ticker"), c.get("period_end")
        if t and per and seen.get(t) and seen[t] != per:
            fresh.append(c)
        if t and per:
            seen[t] = per
    for c in fresh:
        robo.notify("Balanço novo: %s" % c["ticker"], "%s\nO gestor refaz a escolha das ações."
                    % (changes.get(c["ticker"]) or "Trimestre até %s." % c["period_end"]), tags=["bar_chart"])
    if fresh:
        ctx["signals"]["rescreen"] = ctx["now_iso"]
    return "%d empresas lidas, %d com balanço novo" % (len(companies), len(fresh))


def r_copiador(ctx, state):
    res = robo.skill_json("trader-copycat/edgar13f.py", "filings", "1067983", "--limit", "3")
    if res is None or not res.get("filings"):
        raise RuntimeError("trader-copycat não respondeu")
    latest = res["filings"][0]
    known = state.setdefault("copiador", {}).get("accession")
    state["copiador"]["accession"] = latest["accession"]
    if known and known != latest["accession"]:
        robo.notify("Buffett entregou novo 13F", "Berkshire Hathaway, carteira em %s (entregue em %s)."
                    % (latest["period"], latest["filed"]), tags=["briefcase"])
        return "13F novo da Berkshire (%s)" % latest["period"]
    return "último 13F da Berkshire: %s" % latest["period"]


def r_contador(ctx):
    res = robo.skill_json("trader-tax/lots.py", "report")
    if res is None:
        raise RuntimeError("trader-tax não respondeu")
    lots = res.get("lots", [])
    gain = sum(float(l.get("gain") or 0) for l in lots)
    robo.write_json(os.path.join(TIME, "impostos.json"), {"as_of": ctx["now_iso"], "report": res})
    return "%d lotes, resultado não realizado $%.2f" % (len(lots), gain)


# ------------------------------------------------------------------ tick

def tick(dry_run=False):
    robo.clean_secrets()
    if dry_run:
        os.environ["ROBO_DRY_RUN"] = "1"
    now = dt.datetime.now(dt.timezone.utc)
    c = clock()
    is_open = bool(c.get("is_open"))
    state = robo.read_json(os.path.join(TIME, "estado.json"), {}) or {}
    signals = robo.read_json(SIGNALS, {}) or {}
    ny = dt.datetime.fromisoformat(str(c["timestamp"]).replace("Z", "+00:00"))
    today = ny.date()
    signals["bloqueio"] = {s: b for s, b in (signals.get("bloqueio") or {}).items() if b["until"] > today.isoformat()}
    ctx = {"now_iso": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "today": today, "signals": signals, "dry_run": dry_run}
    plan = [
        ("gestor", lambda: r_gestor(ctx), dict(need_open=True, is_open=is_open)),
        ("noticias", lambda: r_noticias(ctx), dict(every_min=30)),
        ("politicos", lambda: r_politicos(ctx), dict(every_min=120)),
        ("vigia", lambda: r_vigia(ctx), dict(need_open=True, is_open=is_open)),
        ("analista", lambda: r_analista(ctx, state), dict(daily_at="07:00")),
        ("copiador", lambda: r_copiador(ctx, state), dict(daily_at="08:00")),
        ("contador", lambda: r_contador(ctx), dict(daily_at="16:30")),
    ]
    # the gestor runs last so it sees this tick's news blocks and re-screen request
    plan = plan[1:] + plan[:1]
    for rid, fn, gate in plan:
        if not due(state, rid, now, ny, **gate):
            continue
        if rid == "gestor":
            robo.write_json(SIGNALS, signals)
        rec = state.setdefault(rid, {})
        try:
            rec["result"] = fn()
            rec["ok"] = True
        except Exception as e:  # one robot failing never stops the others
            rec["result"] = "erro: %s" % str(e)[:200]
            rec["ok"] = False
        rec["last_run"] = ctx["now_iso"]
        rec["last_day"] = today.isoformat()
        print("[%s] %s" % (rid, rec["result"]))
    robo.write_json(SIGNALS, signals)
    robo.write_json(os.path.join(TIME, "estado.json"), state)
    robo.write_json(STATUS, {"as_of": ctx["now_iso"], "market_open": is_open, "bloqueio": signals.get("bloqueio", {}),
                             "robots": [dict(r, **{k: state.get(r["id"], {}).get(k) for k in ("last_run", "result", "ok")})
                                        for r in ROBOTS]})
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="One tick of the robot team.")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    return tick(a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
