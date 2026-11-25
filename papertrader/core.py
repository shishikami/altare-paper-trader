"""Deterministic, long-only local simulation. All money uses Decimal strings."""
from __future__ import annotations

from contextlib import contextmanager, closing
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

SYMBOLS = ("BTC/USD", "ETH/USD")
STRATEGIES = ("trend", "mean_reversion")
ZERO = Decimal("0")


class SafetyError(RuntimeError):
    pass


def dec(value):
    try:
        x = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SafetyError("invalid number") from exc
    if not x.is_finite():
        raise SafetyError("non-finite number")
    return x


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def validate_config(c):
    required = {"mode", "parameters_confirmed", "initial_cash_usd", "fee_bps", "slippage_bps",
                "max_order_usd", "max_gross_fraction", "max_symbol_fraction",
                "max_correlated_fraction", "max_strategy_fraction", "daily_loss_usd",
                "quote_max_age_seconds", "candle_max_age_seconds", "poll_seconds",
                "order_usd", "stop_loss_fraction", "event_enabled", "ai_enabled",
                "max_planned_loss_usd", "max_holdings_initial_fraction"}
    if set(c) != required:
        raise SafetyError("config fields must exactly match example schema")
    if c["mode"] != "paper" or c["event_enabled"] is not False or c["ai_enabled"] is not False:
        raise SafetyError("only local paper; event/AI adapters disabled")
    if type(c["parameters_confirmed"]) is not bool:
        raise SafetyError("parameters_confirmed must be boolean")
    for key in required - {"mode", "parameters_confirmed", "event_enabled", "ai_enabled"}:
        if dec(c[key]) <= 0:
            raise SafetyError("positive config required: " + key)
    for key in ("max_gross_fraction", "max_symbol_fraction", "max_correlated_fraction",
                "max_strategy_fraction", "stop_loss_fraction", "max_holdings_initial_fraction"):
        if dec(c[key]) > 1:
            raise SafetyError("fraction above one: " + key)
    if dec(c["fee_bps"]) > 100 or dec(c["slippage_bps"]) > 500:
        raise SafetyError("unreasonable paper execution costs")
    if dec(c["daily_loss_usd"]) >= dec(c["initial_cash_usd"]):
        raise SafetyError("daily loss must be below initial budget")
    if dec(c["order_usd"]) > dec(c["max_order_usd"]):
        raise SafetyError("order size exceeds cap")
    if not 15 <= dec(c["poll_seconds"]) <= 3600:
        raise SafetyError("poll must be 15..3600 seconds")
    if dec(c["quote_max_age_seconds"]) > 120 or dec(c["candle_max_age_seconds"]) > 3900:
        raise SafetyError("freshness limits too permissive")
    return c


@dataclass(frozen=True)
class Market:
    symbol: str
    price: str
    quote_at: float
    candle_at: int  # opening timestamp of last completed 1h candle
    closes: tuple[str, ...]

    def check(self, now, config):
        if self.symbol not in SYMBOLS or dec(self.price) <= 0:
            raise SafetyError("invalid symbol or price")
        quote_age = dec(now) - dec(self.quote_at)
        candle_age = dec(now) - dec(self.candle_at) - 3600
        if not 0 <= quote_age <= dec(config["quote_max_age_seconds"]):
            raise SafetyError("stale/future quote")
        if not 0 <= candle_age <= dec(config["candle_max_age_seconds"]):
            raise SafetyError("stale/future closed candle")
        if type(self.candle_at) is not int or self.candle_at % 3600 != 0:
            raise SafetyError("unaligned candle timestamp")
        if len(self.closes) < 30 or any(dec(x) <= 0 for x in self.closes):
            raise SafetyError("insufficient/invalid closed candle history")


def equity(s, marks):
    return dec(s["cash"]) + sum((dec(p["qty"]) * dec(marks[p["symbol"]])
                                for p in s["positions"].values()), ZERO)


def signal(strategy, closes, holding):
    """One decision per completed candle; no predictive/AI claims."""
    x = [dec(v) for v in closes]
    fast, slow = sum(x[-5:]) / 5, sum(x[-20:]) / 20
    if strategy == "trend":
        return "buy" if fast > slow and not holding else "sell" if fast < slow and holding else "hold"
    if strategy == "mean_reversion":
        return "buy" if x[-1] < slow * Decimal("0.98") and not holding else "sell" if x[-1] >= slow and holding else "hold"
    raise SafetyError("unsupported/disabled strategy")


class Ledger:
    def __init__(self, directory, config):
        self.root = Path(directory)
        self.config = validate_config(config)
        self.db_path = self.root / "ledger.sqlite3"
        self.stop_path = self.root / "STOP"
        self.manifest = self.root / "IDENTITY.json"
        self.config_hash = digest(canonical(config))

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(self.root.iterdir()):
            raise SafetyError("refuse initialization in nonempty directory")
        # Manifest first: a crash cannot turn a lost database into a new account.
        self.manifest.write_text(canonical({"config_hash": self.config_hash, "schema": 1}))
        self.stop_path.write_text("Initialized stopped; explicit confirmed CLI start required.\n")
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE journal(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL)")
            s = {"cash": str(dec(self.config["initial_cash_usd"])), "positions": {}, "marks": {},
                 "stopped": True, "day": None, "day_open": self.config["initial_cash_usd"],
                 "day_latched": False, "seen": {}, "realized": "0", "fees": "0",
                 "last_cycle": 0, "quote_times": {}, "candle_times": {}, "fill_count": 0,
                 "strategy_cashflows": {k: "0" for k in STRATEGIES},
                 "strategy_realized": {k: "0" for k in STRATEGIES},
                 "strategy_fees": {k: "0" for k in STRATEGIES}}
            self._append(db, "0" * 64, {"type": "init", "config_hash": self.config_hash}, s)

    @staticmethod
    def _append(db, previous, payload, s):
        p, state = canonical(payload), canonical(s)
        h = digest(previous + p + state)
        db.execute("INSERT INTO journal(payload,state,previous,hash) VALUES(?,?,?,?)", (p, state, previous, h))
        return h

    def _read(self, db):
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SafetyError("SQLite integrity failure")
        prev, expected_seq, s = "0" * 64, 1, None
        cash, quantities = dec(self.config["initial_cash_usd"]), {}
        fill_count = 0
        for seq, p, state, parent, h in db.execute("SELECT seq,payload,state,previous,hash FROM journal ORDER BY seq"):
            if seq != expected_seq or parent != prev or digest(parent + p + state) != h:
                raise SafetyError("journal chain corruption")
            event, s = json.loads(p), json.loads(state)
            if seq == 1 and (event.get("type") != "init" or event.get("config_hash") != self.config_hash):
                raise SafetyError("genesis/config mismatch")
            for f in event.get("fills", []):
                fill_count += 1
                if f.get("mode") != "paper" or f.get("sequence") != fill_count:
                    raise SafetyError("non-paper or nonsequential fill")
                cash += dec(f["cash_delta"])
                key = f["strategy"] + ":" + f["symbol"]
                quantities[key] = quantities.get(key, ZERO) + dec(f["qty_delta"])
            actual = {k: dec(v["qty"]) for k, v in s["positions"].items() if dec(v["qty"]) != 0}
            if fill_count != s["fill_count"] or cash != dec(s["cash"]) or actual != {k: v for k, v in quantities.items() if v != 0}:
                raise SafetyError("fill/checkpoint reconciliation failure")
            if cash < 0 or any(v < 0 for v in actual.values()):
                raise SafetyError("negative paper balance")
            prev, expected_seq = h, seq + 1
        if s is None:
            raise SafetyError("missing genesis")
        return s, prev

    @contextmanager
    def transaction(self):
        if not self.manifest.is_file() or not self.db_path.is_file():
            raise SafetyError("missing ledger/identity; never auto-reset")
        if json.loads(self.manifest.read_text()) != {"config_hash": self.config_hash, "schema": 1}:
            raise SafetyError("config changed; explicit migration required")
        # mode=rw prevents creating a missing ledger. Serialize state and control changes.
        db = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            s, prev = self._read(db)
            yield db, s, prev
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def status(self):
        with self.transaction() as (_, s, _):
            strategy_summary = {}
            for strategy in STRATEGIES:
                owned = [p for p in s["positions"].values() if p["strategy"] == strategy]
                value = sum((dec(p["qty"]) * dec(s["marks"][p["symbol"]]) for p in owned), ZERO)
                strategy_summary[strategy] = {"market_value_usd": str(value),
                    "net_pnl_usd": str(value + dec(s["strategy_cashflows"][strategy])),
                    "realized_pnl_usd": s["strategy_realized"][strategy],
                    "fees_usd": s["strategy_fees"][strategy]}
            return {**s, "strategy_summary": strategy_summary, "mode": "paper", "kill_switch": self.stop_path.exists(),
                    "equity_usd": str(equity(s, s["marks"])),
                    "holdings_fixed_initial_cap_usd": str(dec(self.config["initial_cash_usd"]) * dec(self.config["max_holdings_initial_fraction"])),
                    "holdings_current_equity_cap_usd": str(max(ZERO, equity(s, s["marks"])) * dec(self.config["max_holdings_initial_fraction"])),
                    "valuation": "last stored quotes, NOT necessarily current",
                    "parameters_confirmed": self.config["parameters_confirmed"]}

    def stop(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Even a corrupt DB cannot prevent the independent emergency sentinel.
        with self.stop_path.open("w") as f:
            f.write("Manually stopped; no fills including exits.\n")
            f.flush()
            import os
            os.fsync(f.fileno())
        with self.transaction() as (db, s, prev):
            s["stopped"] = True
            self._append(db, prev, {"type": "stop"}, s)

    def start(self, acknowledged=False):
        if not acknowledged or not self.config["parameters_confirmed"]:
            raise SafetyError("confirm example parameters and acknowledge local paper only")
        with self.transaction() as (db, s, prev):
            if self.stop_path.exists():
                self.stop_path.unlink()
            s["stopped"] = False
            self._append(db, prev, {"type": "start"}, s)
        # Crash during start leaves either DB stopped or sentinel present (fail closed).

    def _fill(self, s, strategy, m, side, event_id, now):
        c = self.config
        if strategy not in STRATEGIES or side not in ("buy", "sell"):
            raise SafetyError("unsupported paper order")
        key = strategy + ":" + m.symbol
        p = s["positions"].get(key, {"symbol": m.symbol, "strategy": strategy, "qty": "0", "cost": "0"})
        qty, cost, mark = dec(p["qty"]), dec(p["cost"]), dec(m.price)
        slip, fee_rate = dec(c["slippage_bps"]) / 10000, dec(c["fee_bps"]) / 10000
        price = mark * (1 + slip if side == "buy" else 1 - slip)
        if side == "buy":
            if s["day_latched"]:
                return None, "daily_loss_latched"
            # Do not pyramid. All configured caps include existing positions across strategies.
            if qty:
                return None, "already_holding"
            notional = dec(c["order_usd"])
            if notional > dec(c["max_order_usd"]):
                return None, "max_order"
            q = (notional / price).quantize(Decimal("0.00000001"), rounding="ROUND_DOWN")
            if q <= 0:
                return None, "dust"
            notional, fee = q * price, q * price * fee_rate
            if notional + fee > dec(s["cash"]):
                return None, "cash"
            # Planned risk includes adverse entry, adverse stop execution and both fees.
            # The stop is a reference trigger, NOT a guaranteed execution price.
            stop_price = price * (1 - dec(c["stop_loss_fraction"]))
            stop_execution = stop_price * (1 - slip)
            planned_loss = q * (price - stop_execution) + fee + q * stop_execution * fee_rate
            if planned_loss > dec(c["max_planned_loss_usd"]):
                return None, "planned_stop_loss"
            eq_after = equity(s, s["marks"]) - fee - q * (price - mark)
            gross = sum((dec(v["qty"]) * dec(s["marks"][v["symbol"]]) for v in s["positions"].values()), ZERO)
            symbol_total = sum((dec(v["qty"]) * mark for v in s["positions"].values() if v["symbol"] == m.symbol), ZERO)
            strategy_total = sum((dec(v["qty"]) * dec(s["marks"][v["symbol"]]) for v in s["positions"].values() if v["strategy"] == strategy), ZERO)
            # Never grow the fixed initial-budget ceiling after gains. After losses,
            # also preserve the same cash fraction against current post-fill equity.
            holdings_fraction = dec(c["max_holdings_initial_fraction"])
            holdings_cap = min(dec(c["initial_cash_usd"]) * holdings_fraction,
                               max(ZERO, eq_after) * holdings_fraction)
            if gross + q * mark > holdings_cap:
                return None, "holdings_initial_or_equity_cap"
            for value, limit, reason in [(gross + q * mark, "max_gross_fraction", "gross"),
                                          (gross + q * mark, "max_correlated_fraction", "correlated_BTC_ETH"),
                                          (symbol_total + q * mark, "max_symbol_fraction", "symbol"),
                                          (strategy_total + q * mark, "max_strategy_fraction", "strategy")]:
                if value > eq_after * dec(c[limit]):
                    return None, reason
            cash_delta, qty_delta = -(notional + fee), q
            p["qty"], p["cost"] = str(q), str(notional + fee)
            p["stop_price"] = str(stop_price)
        else:
            if qty <= 0:
                return None, "no_position"
            # Split exits at max_order, with any residual handled on the next poll.
            q = min(qty, (dec(c["max_order_usd"]) / price).quantize(Decimal("0.00000001"), rounding="ROUND_DOWN"))
            if q <= 0:
                return None, "dust"
            notional, fee = q * price, q * price * fee_rate
            cash_delta, qty_delta = notional - fee, -q
            removed_cost = cost * q / qty
            s["realized"] = str(dec(s["realized"]) + cash_delta - removed_cost)
            s["strategy_realized"][strategy] = str(dec(s["strategy_realized"][strategy]) + cash_delta - removed_cost)
            p["qty"], p["cost"] = str(qty - q), str(cost - removed_cost)
        s["cash"] = str(dec(s["cash"]) + cash_delta)
        s["fees"] = str(dec(s["fees"]) + fee)
        s["strategy_cashflows"][strategy] = str(dec(s["strategy_cashflows"][strategy]) + cash_delta)
        s["strategy_fees"][strategy] = str(dec(s["strategy_fees"][strategy]) + fee)
        if dec(p["qty"]) == 0:
            s["positions"].pop(key, None)
        else:
            s["positions"][key] = p
        s["fill_count"] += 1
        return {"id": event_id + ":fill:" + str(s["fill_count"]), "decision_id": event_id,
                "sequence": s["fill_count"],
                "planned_loss_usd": str(planned_loss) if side == "buy" else None,
                "stop_price": p["stop_price"], "at": now, "strategy": strategy, "symbol": m.symbol, "side": side,
                "qty_delta": str(qty_delta), "cash_delta": str(cash_delta), "price": str(price),
                "fee": str(fee), "reference_price": m.price, "mode": "paper"}, None

    def cycle(self, markets, now):
        if set(markets) != set(SYMBOLS):
            raise SafetyError("complete correlated portfolio quote set required")
        for symbol, m in markets.items():
            if symbol != m.symbol:
                raise SafetyError("market key mismatch")
            m.check(now, self.config)
        with self.transaction() as (db, s, prev):
            if s["stopped"] or self.stop_path.exists():
                raise SafetyError("persistent kill switch active")
            if dec(now) < dec(s["last_cycle"]):
                raise SafetyError("clock moved backwards")
            for symbol, m in markets.items():
                if dec(m.quote_at) < dec(s["quote_times"].get(symbol, 0)) or m.candle_at < s["candle_times"].get(symbol, 0):
                    raise SafetyError("market timestamps moved backwards")
            s["last_cycle"] = now
            s["quote_times"] = {k: v.quote_at for k, v in markets.items()}
            s["candle_times"] = {k: v.candle_at for k, v in markets.items()}
            # Day boundary uses prior marked equity so overnight loss is not hidden.
            day = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).date().isoformat()
            if s["day"] != day:
                s["day"], s["day_open"], s["day_latched"] = day, str(equity(s, s["marks"])), False
            s["marks"] = {k: v.price for k, v in markets.items()}
            fills, decisions = [], []
            for strategy in STRATEGIES:
                for symbol in SYMBOLS:
                    if dec(s["day_open"]) - equity(s, s["marks"]) >= dec(self.config["daily_loss_usd"]):
                        s["day_latched"] = True
                    m, key = markets[symbol], strategy + ":" + symbol
                    holding = key in s["positions"]
                    emergency = holding and dec(m.price) <= dec(s["positions"][key]["stop_price"])
                    # A previous partial stop exit remains an exit until fully flat.
                    exiting = holding and s["positions"][key].get("exiting", False)
                    event_id = key + ":" + str(m.candle_at)
                    if not emergency and not exiting and s["seen"].get(key) == event_id:
                        continue
                    action = "sell" if emergency or exiting else signal(strategy, m.closes, holding)
                    s["seen"][key] = event_id  # Persist rejected/hold decisions too.
                    reason = None
                    if action != "hold":
                        if action == "sell" and holding:
                            s["positions"][key]["exiting"] = True
                        fill, reason = self._fill(s, strategy, m, action, event_id, now)
                        if fill:
                            fills.append(fill)
                    decisions.append({"id": event_id, "action": action, "reason": reason})
            if dec(s["day_open"]) - equity(s, s["marks"]) >= dec(self.config["daily_loss_usd"]):
                s["day_latched"] = True
            if self.stop_path.exists():
                raise SafetyError("kill switch appeared; cycle rolled back")
            event = {"type": "cycle", "at": now, "fills": fills, "decisions": decisions}
            self._append(db, prev, event, s)
            return event
