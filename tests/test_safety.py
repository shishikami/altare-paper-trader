"""All prices here are SYNTHETIC fixtures, not market history or backtests."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from papertrader.core import Ledger, Market, SafetyError, SYMBOLS, canonical, dec, signal, validate_config
from papertrader.feed import PublicFeed, NoRedirect
from papertrader.__main__ import main

EXAMPLE = json.loads((Path(__file__).resolve().parents[1] / "config/example.json").read_text())
NOW = 1795626000


def markets(now=NOW, price="100", trend=True):
    closes = tuple(["100"] * 25 + (["110"] * 5 if trend else ["90"] * 5))
    candle = (int(now) // 3600 - 1) * 3600
    return {s: Market(s, price, now - 1, candle, closes) for s in SYMBOLS}


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.socket_patch = patch("socket.create_connection", side_effect=AssertionError("offline suite forbids network"))
        self.socket_patch.start()
        self.addCleanup(self.socket_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "paper"
        self.c = copy.deepcopy(EXAMPLE)
        self.c["parameters_confirmed"] = True
        self.ledger = Ledger(self.root, self.c)
        self.ledger.initialize()
        self.ledger.start(True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_is_stopped_and_unconfirmed(self):
        other = Ledger(Path(self.tmp.name) / "default", EXAMPLE)
        other.initialize()
        self.assertTrue(other.status()["stopped"])
        self.assertTrue(other.status()["kill_switch"])
        with self.assertRaises(SafetyError):
            other.start(True)
        with self.assertRaises(SafetyError):
            other.cycle(markets(), NOW)

    def test_restart_replays_and_deduplicates(self):
        event = self.ledger.cycle(markets(), NOW)
        self.assertEqual(len(event["fills"]), 2)
        before = self.ledger.status()
        reopened = Ledger(self.root, self.c)
        self.assertEqual(before, reopened.status())
        repeat = reopened.cycle(markets(), NOW)
        self.assertEqual(repeat["fills"], [])
        self.assertEqual(before["cash"], reopened.status()["cash"])

    def test_fee_slippage_and_rounding(self):
        fills = self.ledger.cycle(markets(), NOW)["fills"]
        expected_cash = dec("1000")
        for f in fills:
            self.assertEqual(f["mode"], "paper")
            self.assertEqual(dec(f["price"]), dec("100.1"))
            quantity = dec(f["qty_delta"])
            self.assertLessEqual(quantity * dec(f["price"]), dec("25"))
            self.assertEqual(dec(f["fee"]), quantity * dec(f["price"]) * dec("0.0026"))
            expected_cash += dec(f["cash_delta"])
        self.assertEqual(dec(self.ledger.status()["cash"]), expected_cash)
        self.assertGreater(dec(self.ledger.status()["fees"]), 0)

    def test_stop_persistent_and_independent(self):
        self.ledger.stop()
        self.assertTrue(Ledger(self.root, self.c).status()["stopped"])
        # Removing the sentinel alone is not an activation path.
        self.ledger.stop_path.unlink()
        with self.assertRaises(SafetyError):
            self.ledger.cycle(markets(), NOW)

    def test_file_stop_alone_and_unacknowledged_start(self):
        self.ledger.stop_path.write_text("emergency")
        with self.assertRaises(SafetyError):
            self.ledger.cycle(markets(), NOW)
        with self.assertRaises(SafetyError):
            self.ledger.start(False)

    def test_stop_during_cycle_rolls_back(self):
        original = self.ledger._fill
        before = self.ledger.status()
        def interrupted(*args):
            result = original(*args)
            self.ledger.stop_path.write_text("emergency")
            return result
        with patch.object(self.ledger, "_fill", side_effect=interrupted):
            with self.assertRaises(SafetyError):
                self.ledger.cycle(markets(), NOW)
        self.assertEqual(self.ledger.status()["cash"], before["cash"])
        self.assertEqual(self.ledger.status()["positions"], {})

    def test_stale_future_nonfinite_and_missing_quotes(self):
        cases = []
        valid = markets()
        for field, value in [("quote_at", NOW - 1000), ("quote_at", NOW + 60),
                             ("candle_at", valid["BTC/USD"].candle_at - 7200),
                             ("price", "NaN"), ("price", "0"), ("quote_at", float("nan")),
                             ("closes", ("100",)), ("candle_at", valid["BTC/USD"].candle_at + 1)]:
            m = valid["BTC/USD"]
            changed = Market(**{**m.__dict__, field: value})
            cases.append({**valid, "BTC/USD": changed})
        cases.append({"BTC/USD": valid["BTC/USD"]})
        for values in cases:
            with self.subTest(values=values), self.assertRaises(SafetyError):
                self.ledger.cycle(values, NOW)
        self.assertEqual(self.ledger.status()["positions"], {})

    def test_daily_loss_latches_blocks_buys_allows_exits(self):
        self.ledger.cycle(markets(), NOW)
        event = self.ledger.cycle(markets(NOW + 60, "40", trend=False), NOW + 60)
        self.assertEqual(len(event["fills"]), 2)
        self.assertTrue(all(f["side"] == "sell" for f in event["fills"]))
        self.assertTrue(self.ledger.status()["day_latched"])
        later = NOW + 3600
        second = self.ledger.cycle(markets(later), later)
        self.assertEqual(second["fills"], [])
        self.assertTrue(any(d["reason"] == "daily_loss_latched" for d in second["decisions"]))
        tomorrow = NOW + 86400
        self.ledger.cycle(markets(tomorrow), tomorrow)
        self.assertFalse(self.ledger.status()["day_latched"])

    def test_all_exposure_caps_and_cash(self):
        for key in ("max_gross_fraction", "max_correlated_fraction", "max_symbol_fraction", "max_strategy_fraction", "initial_cash_usd"):
            c = copy.deepcopy(self.c)
            if key == "initial_cash_usd":
                c[key], c["daily_loss_usd"] = "21", "1"
            else:
                c[key] = "0.001"
            other = Ledger(Path(self.tmp.name) / key, c)
            other.initialize()
            other.start(True)
            event = other.cycle(markets(), NOW)
            self.assertEqual(event["fills"], [], key)
            self.assertTrue(any(d["reason"] for d in event["decisions"]), key)

    def test_correlated_and_strategy_caps_aggregate_positions(self):
        c = {**self.c, "max_correlated_fraction": "0.04"}
        other = Ledger(Path(self.tmp.name) / "correlated", c)
        other.initialize()
        other.start(True)
        event = other.cycle(markets(), NOW)
        self.assertEqual(len(event["fills"]), 1)
        self.assertTrue(any(d["reason"] == "correlated_BTC_ETH" for d in event["decisions"]))
        c = {**self.c, "max_strategy_fraction": "0.04"}
        other = Ledger(Path(self.tmp.name) / "strategy", c)
        other.initialize()
        other.start(True)
        self.assertEqual(len(other.cycle(markets(), NOW)["fills"]), 1)

    def test_invalid_config_and_no_live_mode(self):
        for key, value in [("mode", "live"), ("event_enabled", True), ("ai_enabled", True),
                           ("order_usd", "51"), ("max_order_usd", "-1"), ("fee_bps", "NaN"),
                           ("max_gross_fraction", "1.1"), ("parameters_confirmed", "yes"),
                           ("quote_max_age_seconds", 99999)]:
            with self.subTest(key=key), self.assertRaises(SafetyError):
                validate_config({**self.c, key: value})
        with self.assertRaises(SafetyError):
            validate_config({**self.c, "api_key": "NOT_A_REAL_KEY"})
        for operation in ("AddOrder", "private/Balance", "https://example.org", "Withdraw"):
            with self.assertRaises(SafetyError):
                PublicFeed().request(operation, "BTC/USD")
        with self.assertRaises(SafetyError):
            signal("event", ["100"] * 30, False)

    def test_config_change_refused(self):
        other = Ledger(self.root, {**self.c, "daily_loss_usd": "10"})
        with self.assertRaises(SafetyError):
            other.status()

    def test_missing_db_never_reinitializes(self):
        self.ledger.db_path.unlink()
        with self.assertRaises(SafetyError):
            self.ledger.status()
        self.assertFalse(self.ledger.db_path.exists())
        with self.assertRaises(SafetyError):
            self.ledger.initialize()

    def test_corrupt_sqlite_and_stop_still_works(self):
        self.ledger.db_path.write_bytes(b"not a database")
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.status()
        with self.assertRaises(sqlite3.DatabaseError):
            self.ledger.stop()
        self.assertTrue(self.ledger.stop_path.exists())

    def test_modified_checkpoint_detected(self):
        with closing(sqlite3.connect(self.ledger.db_path)) as db, db:
            db.execute("UPDATE journal SET state='{}' WHERE seq=1")
        with self.assertRaises(SafetyError):
            self.ledger.status()

    def test_reconciliation_even_if_checkpoint_hash_recomputed(self):
        # Accidental inconsistent checkpoint still rejected even with valid chain hash.
        from papertrader.core import digest
        with closing(sqlite3.connect(self.ledger.db_path)) as db, db:
            p, raw, prev = db.execute("SELECT payload,state,previous FROM journal WHERE seq=2").fetchone()
            state = json.loads(raw)
            state["cash"] = "9999"
            raw = canonical(state)
            db.execute("UPDATE journal SET state=?,hash=? WHERE seq=2", (raw, digest(prev + p + raw)))
        with self.assertRaises(SafetyError):
            self.ledger.status()

    def test_transaction_failure_is_atomic(self):
        original = self.ledger._append
        def fail(db, prev, event, s):
            original(db, prev, event, s)
            raise SafetyError("synthetic crash before commit")
        with patch.object(self.ledger, "_append", side_effect=fail):
            with self.assertRaises(SafetyError):
                self.ledger.cycle(markets(), NOW)
        self.assertEqual(self.ledger.status()["cash"], "1000")
        self.assertEqual(len(self.ledger.cycle(markets(), NOW)["fills"]), 2)

    def test_price_rise_split_exit_stays_exit_only(self):
        self.ledger.cycle(markets(), NOW)
        # Trend sell at a far larger price requires multiple max-50 paper fills.
        first = self.ledger.cycle(markets(NOW + 3600, "1000", False), NOW + 3600)
        self.assertTrue(all(dec(f["price"]) * abs(dec(f["qty_delta"])) <= 50 for f in first["fills"]))
        self.assertTrue(self.ledger.status()["positions"])
        # Same candle now bullish: persistent exiting flag must prevent holding/rebuy.
        second = self.ledger.cycle(markets(NOW + 3660, "1000", True), NOW + 3660)
        self.assertTrue(second["fills"])
        self.assertTrue(all(f["side"] == "sell" for f in second["fills"]))
        ids = [f["id"] for f in first["fills"] + second["fills"]]
        self.assertEqual(len(ids), len(set(ids)))
        Ledger(self.root, self.c).status()  # independent replay after partial fills

    def test_clock_and_quote_regression_refused(self):
        self.ledger.cycle(markets(), NOW)
        with self.assertRaises(SafetyError):
            self.ledger.cycle(markets(NOW - 1), NOW - 1)
        prior = markets()
        m = prior["BTC/USD"]
        prior["BTC/USD"] = Market(**{**m.__dict__, "quote_at": NOW - 2})
        with self.assertRaises(SafetyError):
            self.ledger.cycle(prior, NOW + 1)

    def test_offline_cli_paused_does_not_fetch(self):
        self.ledger.stop()
        config = Path(self.tmp.name) / "config.json"
        config.write_text(json.dumps(self.c))
        with patch.object(PublicFeed, "snapshot", side_effect=AssertionError("no network")):
            self.assertEqual(main(["--config", str(config), "--data", str(self.root), "run", "--once"]), 0)

    def test_network_failure_no_mutation(self):
        from urllib.error import URLError
        before = self.ledger.status()
        with patch("papertrader.feed.build_opener") as opener:
            opener.return_value.open.side_effect = URLError("synthetic outage")
            with self.assertRaises(SafetyError):
                PublicFeed().snapshot()
        self.assertEqual(before, self.ledger.status())

    def test_feed_discards_uncommitted_and_rejects_gap(self):
        def fixture(operation, symbol):
            if operation == "Trades":
                return [["100", "1", NOW - 1]]
            start = (NOW // 3600 - 31) * 3600
            rows = [[start + n * 3600, "100", "100", "100", "100"] for n in range(31)]
            rows[-1][4] = "999999"  # Current uncommitted candle must not leak into signal.
            return rows
        feed = PublicFeed()
        with patch.object(feed, "request", side_effect=fixture):
            snap = feed.snapshot()
        self.assertEqual(snap["BTC/USD"].closes[-1], "100")
        def gap(op, symbol):
            rows = fixture(op, symbol)
            if op == "OHLC":
                rows[4][0] += 10
            return rows
        with patch.object(feed, "request", side_effect=gap), self.assertRaises(SafetyError):
            feed.snapshot()
        with self.assertRaises(SafetyError):
            NoRedirect().redirect_request(None, None, None, None, None, None)

    def test_deterministic_multiday_replay(self):
        import random
        rng = random.Random(12345)
        for n in range(100):
            now = NOW + n * 3600
            snap = markets(now, str(rng.randint(30, 200)), trend=bool(n % 2))
            self.ledger.cycle(snap, now)
            state = Ledger(self.root, self.c).status()
            self.assertGreaterEqual(dec(state["cash"]), 0)
            self.assertTrue(all(dec(p["qty"]) >= 0 for p in state["positions"].values()))
            pnl = sum((dec(x["net_pnl_usd"]) for x in state["strategy_summary"].values()), dec(0))
            # Decimal sums can differ in the final place due to operation ordering.
            self.assertLess(abs(pnl - (dec(state["equity_usd"]) - 1000)), dec("1e-20"))

    def test_second_runner_refused_without_feed(self):
        import fcntl
        config = Path(self.tmp.name) / "config.json"
        config.write_text(json.dumps(self.c))
        with (self.root / "runner.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(PublicFeed, "snapshot", side_effect=AssertionError("no network")):
                self.assertEqual(main(["--config", str(config), "--data", str(self.root), "run", "--once"]), 1)

    def test_mean_reversion_buy_and_exit(self):
        closes = ["100"] * 29 + ["90"]
        self.assertEqual(signal("mean_reversion", closes, False), "buy")
        self.assertEqual(signal("mean_reversion", ["100"] * 30, True), "sell")
        self.assertEqual(signal("trend", ["100"] * 30, False), "hold")


if __name__ == "__main__":
    unittest.main()
