"""Operations CLI. 'start' arms only; 'run' is the foreground service."""
import argparse
import fcntl
import json
import signal
import sqlite3
import sys
import time
from .core import Ledger, SafetyError, canonical
from .feed import PublicFeed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Local paper only. Not investment advice.")
    parser.add_argument("--config", default="config/example.json")
    parser.add_argument("--data", default="var/paper")
    parser.add_argument("command", choices=("init", "status", "report", "start", "stop", "run", "market-check"))
    parser.add_argument("--ack-paper-only", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        with open(args.config) as f:
            ledger = Ledger(args.data, json.load(f))
        if args.command == "init":
            ledger.initialize()
            print(canonical({"type": "init", "stopped": True, "mode": "paper"}))
        elif args.command in ("status", "report"):
            print(canonical(ledger.status()))
        elif args.command == "stop":
            ledger.stop()
            print(canonical({"type": "stop", "stopped": True}))
        elif args.command == "start":
            ledger.start(args.ack_paper_only)
            print(canonical({"type": "armed", "mode": "paper", "note": "run foreground separately"}))
        elif args.command == "market-check":
            now_markets = PublicFeed().snapshot()
            now = time.time()
            for m in now_markets.values():
                m.check(now, ledger.config)
            print(canonical({"type": "public_market_check", "observed_at": now,
                             "markets": {k: {"price": m.price, "quote_at": m.quote_at,
                                            "closed_candle_at": m.candle_at} for k, m in now_markets.items()}}))
        else:
            # Fail on corrupt/missing DB BEFORE touching the public network.
            ledger.status()
            lock = (ledger.root / "runner.lock").open("a")
            with lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                running = True

                def shutdown(*_):
                    nonlocal running
                    running = False

                signal.signal(signal.SIGTERM, shutdown)
                signal.signal(signal.SIGINT, shutdown)
                while running:
                    try:
                        state = ledger.status()
                        if state["stopped"] or state["kill_switch"]:
                            event = {"type": "paused", "stopped": True}
                        else:
                            markets = PublicFeed().snapshot()
                            event = ledger.cycle(markets, time.time())
                        print(canonical(event), flush=True)
                    except SafetyError as exc:
                        # No fills on failed snapshot. All-data outage never reuses stored marks.
                        print(canonical({"type": "cycle_refused", "reason": str(exc)}), flush=True)
                        if args.once:
                            return 1
                    if args.once:
                        break
                    deadline = time.monotonic() + float(ledger.config["poll_seconds"])
                    while running and time.monotonic() < deadline:
                        time.sleep(min(1, max(0, deadline - time.monotonic())))
        return 0
    except (SafetyError, sqlite3.Error, OSError, ValueError, KeyError, TypeError) as exc:
        print(canonical({"type": "fatal_refusal", "error_class": type(exc).__name__, "reason": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
