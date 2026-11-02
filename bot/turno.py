#!/usr/bin/env python3
"""One trading shift: runs bot/robo.py every 30 minutes for as long as the market is open.

GitHub's scheduler starts jobs late or drops them, so the workflow starts this shift a few times
a day instead of one job per run. Inside the shift the timing is ours: a run on the :05 and :35
New York half hours, the robot's files saved and pushed after each run, and an exit when the
market closes or the job's time budget runs out (the next scheduled shift then takes over).

    python3 bot/turno.py               # until the close or --max-minutes
    python3 bot/turno.py --once        # a single run, as before
"""

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADE_URL = os.environ.get("ROBO_TEST_TRADE_URL") or "https://paper-api.alpaca.markets"


def clock():
    req = urllib.request.Request(TRADE_URL + "/v2/clock", headers={
        "APCA-API-KEY-ID": " ".join(os.environ.get("ALPACA_API_KEY", "").split()),
        "APCA-API-SECRET-KEY": " ".join(os.environ.get("ALPACA_SECRET_KEY", "").split())})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def save(push):
    """Commit and push what the run wrote (the workflow's old save step, once per run)."""
    def git(*a):
        return subprocess.run(["git"] + list(a), cwd=ROOT, capture_output=True, text=True)

    git("add", "trader", "painel")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-m", "robô: rodada %s" % dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"))
    if push:
        for _ in range(3):
            if git("pull", "--rebase", "--quiet").returncode == 0 and git("push", "--quiet").returncode == 0:
                return
            time.sleep(5)
        print("aviso: não consegui enviar o estado ao GitHub", file=sys.stderr)


def run_robot(args):
    # a fresh process each time, so code merged during the shift is picked up on the next run
    subprocess.run([sys.executable, os.path.join(ROOT, "bot", "robo.py")] + args, cwd=ROOT)


def next_slot(now, minutes=(5, 35)):
    """The next :05 or :35 after now."""
    for add in range(0, 61):
        t = (now + dt.timedelta(minutes=add)).replace(second=0, microsecond=0)
        if t > now and t.minute in minutes:
            return t
    return now + dt.timedelta(minutes=30)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run the robot through one market session.")
    ap.add_argument("--once", action="store_true", help="a single run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-minutes", type=float, default=340, help="stop after this long (job limit is 360)")
    ap.add_argument("--no-push", action="store_true", help="commit only (tests)")
    a = ap.parse_args(argv)
    robot_args = ["--dry-run"] if a.dry_run else []
    if a.once:
        run_robot(robot_args)
        save(not a.no_push)
        return 0
    deadline = time.monotonic() + a.max_minutes * 60
    ran_today = False
    while time.monotonic() < deadline:
        c = clock()
        now = ts(c["timestamp"])
        if c["is_open"]:
            run_robot(robot_args)
            save(not a.no_push)
            ran_today = True
            wake = next_slot(dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10))
        else:
            opens = ts(c["next_open"])
            if ran_today or opens.date() != now.date():
                print("bolsa fechada; fim do turno")
                return 0
            wake = opens + dt.timedelta(minutes=2)
            print("bolsa abre às %s; esperando" % opens.isoformat())
        wait = (wake - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if time.monotonic() + wait >= deadline:
            break
        time.sleep(max(wait, 5))
    print("fim do tempo deste turno; o próximo turno agendado continua")
    return 0


if __name__ == "__main__":
    sys.exit(main())
