"""User-authorized limits; all runtime examples here remain synthetic and offline."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from papertrader.core import Ledger, Market, SafetyError, SYMBOLS, dec

CONFIG = json.loads((Path(__file__).resolve().parents[1] / 'config/example.json').read_text())
NOW = 1795626000


def snapshot(now=NOW, price='100'):
    candle = (int(now) // 3600 - 1) * 3600
    return {s: Market(s, price, now - 1, candle, tuple(['100'] * 25 + ['110'] * 5)) for s in SYMBOLS}


class ConfirmedRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.network = patch('socket.create_connection', side_effect=AssertionError('offline'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.number = 0

    def config(self, **overrides):
        # Looser sublimits ONLY in isolated synthetic tests to reach the confirmed caps.
        # These are never saved to production/example config or used with real prices.
        c = {**CONFIG, 'parameters_confirmed': True, 'max_order_usd': '4000',
             'order_usd': '1001', 'max_gross_fraction': '1', 'max_symbol_fraction': '1',
             'max_correlated_fraction': '1', 'max_strategy_fraction': '1',
             'stop_loss_fraction': '0.01'}
        return {**c, **overrides}

    def ledger(self, c):
        self.number += 1
        ledger = Ledger(Path(self.temp.name) / str(self.number), c)
        ledger.initialize()
        ledger.start(True)
        return ledger

    def one_fill(self, ledger, price='100', now=NOW):
        # First buy is trend BTC; forbid other strategy signals for isolated cap tests.
        with patch('papertrader.core.signal', side_effect=['buy', 'hold', 'hold', 'hold']):
            return ledger.cycle(snapshot(now, price), now)

    def test_confirmed_values_do_not_arm_or_raise_other_sublimits(self):
        self.assertEqual(CONFIG['initial_cash_usd'], '4800')
        self.assertEqual(CONFIG['daily_loss_usd'], '96')
        self.assertEqual(CONFIG['max_planned_loss_usd'], '48')
        self.assertEqual(dec(CONFIG['initial_cash_usd']) * dec(CONFIG['max_holdings_initial_fraction']), 3840)
        self.assertFalse(CONFIG['parameters_confirmed'])
        self.assertEqual(CONFIG['max_order_usd'], '50')
        self.assertEqual(CONFIG['max_gross_fraction'], '0.50')
        self.assertEqual(CONFIG['max_correlated_fraction'], '0.40')
        ledger = Ledger(Path(self.temp.name) / 'unconfirmed', CONFIG)
        ledger.initialize()
        self.assertEqual(ledger.status()['cash'], '4800')
        self.assertTrue(ledger.status()['stopped'])
        with self.assertRaises(SafetyError):
            ledger.start(True)

    def test_planned_risk_includes_both_costs_and_adverse_stop_slippage(self):
        c = self.config(order_usd='900.9', stop_loss_fraction='0.05')  # exactly q=9 at entry 100.1
        ledger = self.ledger(c)
        event = self.one_fill(ledger)
        # Price-to-trigger loss alone is below $48, but fees + exit slippage breach it.
        entry = dec('100.1')
        trigger = entry * dec('0.95')
        exit_price = trigger * dec('0.999')
        self.assertLess(9 * (entry - trigger), 48)
        planned = 9 * (entry - exit_price) + 9 * entry * dec('0.0026') + 9 * exit_price * dec('0.0026')
        self.assertGreater(planned, 48)
        self.assertEqual(event['fills'], [])
        self.assertEqual(event['decisions'][0]['reason'], 'planned_stop_loss')
        accepted = self.one_fill(self.ledger(self.config(order_usd='800.8', stop_loss_fraction='0.05')))
        fill = accepted['fills'][0]
        self.assertLessEqual(dec(fill['planned_loss_usd']), 48)
        self.assertEqual(dec(fill['stop_price']), trigger)

    def test_persisted_entry_stop_controls_protective_exit(self):
        c = self.config(order_usd='800.8', stop_loss_fraction='0.05')
        ledger = self.ledger(c)
        fill = self.one_fill(ledger)['fills'][0]
        saved = Ledger(ledger.root, c).status()['positions']['trend:BTC/USD']['stop_price']
        self.assertEqual(saved, fill['stop_price'])
        # 95.20 is above actual recorded 95.095 trigger, though below the former
        # fee-inflated cost-basis stop. Fees belong in risk, not a moving trigger.
        first = ledger.cycle(snapshot(NOW + 60, '95.20'), NOW + 60)
        self.assertEqual(first['fills'], [])
        second = ledger.cycle(snapshot(NOW + 120, '95.09'), NOW + 120)
        self.assertEqual(len(second['fills']), 1)
        self.assertEqual(second['fills'][0]['side'], 'sell')
        self.assertEqual(second['fills'][0]['stop_price'], saved)

    def test_planned_risk_exact_boundary_inclusive(self):
        base = self.config(order_usd='800.8', stop_loss_fraction='0.05')
        q, entry, fee, slip = dec(8), dec('100.1'), dec('0.0026'), dec('0.001')
        trigger = entry * dec('0.95')
        exit_price = trigger * (1 - slip)
        boundary = q * (entry - exit_price) + q * entry * fee + q * exit_price * fee
        # Verify comparator at exact deterministic model risk (below the authorized 48).
        yes = self.one_fill(self.ledger({**base, 'max_planned_loss_usd': str(boundary)}))
        no = self.one_fill(self.ledger({**base, 'max_planned_loss_usd': str(boundary - dec('0.000000001'))}))
        self.assertEqual(dec(yes['fills'][0]['planned_loss_usd']), boundary)
        self.assertEqual(no['fills'], [])

    def test_fixed_initial_cap_rejects_more_than_3840_even_after_profit(self):
        # A small stop gap isolates holdings cap; normal config keeps its tighter
        # sublimits. q=10 at execution 100.1, then mark rises to 450.
        c = self.config(order_usd='1001', stop_loss_fraction='0.001')
        ledger = self.ledger(c)
        self.assertEqual(len(self.one_fill(ledger)['fills']), 1)
        later = NOW + 3600
        event = ledger.cycle(snapshot(later, '450'), later)
        self.assertGreater(dec(ledger.status()['equity_usd']), 4800)
        self.assertEqual(event['fills'], [])
        self.assertTrue(any(d['reason'] == 'holdings_initial_or_equity_cap' for d in event['decisions']))
        # Appreciation alone must not silently liquidate or resize a position.
        self.assertEqual(dec(ledger.status()['positions']['trend:BTC/USD']['qty']), 10)

    def test_holdings_boundary_and_current_equity_protection(self):
        # With tiny positive costs, holdings just under initial 3840 can still violate
        # 80% of post-fill equity. It must NOT pass only because fixed budget allows it.
        ledger = self.ledger(self.config(order_usd='3840', stop_loss_fraction='0.001'))
        refused = self.one_fill(ledger)
        self.assertEqual(refused['fills'], [])
        self.assertEqual(refused['decisions'][0]['reason'], 'holdings_initial_or_equity_cap')
        # Slightly smaller gross order preserves >20% cash/current-equity buffer.
        ledger = self.ledger(self.config(order_usd='3830', stop_loss_fraction='0.001'))
        accepted = self.one_fill(ledger)
        self.assertEqual(len(accepted['fills']), 1)
        state = ledger.status()
        held = sum((dec(p['qty']) * dec(state['marks'][p['symbol']]) for p in state['positions'].values()), dec(0))
        self.assertLessEqual(held, 3840)
        self.assertLessEqual(held, dec(state['equity_usd']) * dec('0.8'))
        self.assertGreaterEqual(dec(state['cash']), dec(state['equity_usd']) * dec('0.2'))

    def test_marked_loss_reduces_cap_below_fixed_initial_3840(self):
        ledger = self.ledger(self.config(order_usd='1001', stop_loss_fraction='0.01', daily_loss_usd='1000'))
        # Build three independently accounted positions, with real paper fills.
        with patch('papertrader.core.signal', side_effect=['buy', 'buy', 'buy', 'hold']):
            self.assertEqual(len(ledger.cycle(snapshot(), NOW)['fills']), 3)
        original = ledger._fill
        def hold_protective_exits(s, strategy, market, side, event_id, now):
            if side == 'sell':
                return None, 'synthetic_exit_held'
            return original(s, strategy, market, side, event_id, now)
        # Inspect unrealized-loss cap before exits. Simulated exposure is below $3840,
        # but the fourth $1001 buy must not pass 80% of current post-loss equity.
        later = NOW + 3600
        with patch.object(ledger, '_fill', side_effect=hold_protective_exits), patch('papertrader.core.signal', return_value='buy'):
            event = ledger.cycle(snapshot(later, '94'), later)
        state = ledger.status()
        self.assertLess(dec(state['equity_usd']), 4800)
        self.assertEqual(event['fills'], [])
        self.assertTrue(any(d['reason'] == 'holdings_initial_or_equity_cap' for d in event['decisions']))
        candidate_gross = 3 * dec('10') * 94 + dec('1001') / (94 * dec('1.001')) * 94
        self.assertLess(candidate_gross, 3840)
        self.assertGreater(candidate_gross, dec(state['equity_usd']) * dec('0.8'))

    def test_cash_after_fees_must_cover_order(self):
        ledger = self.ledger(self.config(order_usd='4799', max_order_usd='5000', stop_loss_fraction='0.001'))
        event = self.one_fill(ledger)
        self.assertEqual(event['fills'], [])
        self.assertEqual(event['decisions'][0]['reason'], 'cash')

    def test_daily_96_includes_realized_unrealized_costs_and_persists(self):
        c = self.config(stop_loss_fraction='0.03')
        ledger = self.ledger(c)
        self.one_fill(ledger)
        state = ledger.status()
        q = dec(state['positions']['trend:BTC/USD']['qty'])
        # Mark equity EXACTLY 96 below day open before additional protective sell costs.
        boundary_price = (dec('4704') - dec(state['cash'])) / q
        now = NOW + 60
        event = ledger.cycle(snapshot(now, str(boundary_price)), now)
        self.assertTrue(ledger.status()['day_latched'])
        self.assertTrue(all(f['side'] == 'sell' for f in event['fills']))
        state = Ledger(ledger.root, c).status()
        self.assertTrue(state['day_latched'])
        self.assertGreaterEqual(dec(state['day_open']) - dec(state['equity_usd']), 96)
        # Loss cannot be forgotten by process restart or a later recovery in marks.
        next_event = Ledger(ledger.root, c).cycle(snapshot(NOW + 3600), NOW + 3600)
        self.assertEqual(next_event['fills'], [])
        self.assertTrue(any(d['reason'] == 'daily_loss_latched' for d in next_event['decisions']))

    def test_daily_just_below_96_does_not_latch_and_shanghai_rollover(self):
        c = self.config(stop_loss_fraction='0.03')
        ledger = self.ledger(c)
        t = int(datetime(2026, 11, 26, 23, 59, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
        self.one_fill(ledger, now=t)
        state = ledger.status()
        q = dec(state['positions']['trend:BTC/USD']['qty'])
        price = (dec('4704.01') - dec(state['cash'])) / q
        # Disable synthetic sells to inspect a pure unrealized boundary, not claim fill results.
        with patch.object(ledger, '_fill', return_value=(None, 'synthetic_exit_held')):
            ledger.cycle(snapshot(t + 10, str(price)), t + 10)
        self.assertFalse(ledger.status()['day_latched'])
        self.assertEqual(ledger.status()['day'], '2026-11-26')
        old_equity = dec(ledger.status()['equity_usd'])
        with patch.object(ledger, '_fill', return_value=(None, 'synthetic_exit_held')):
            ledger.cycle(snapshot(t + 120, str(price)), t + 120)
        state = Ledger(ledger.root, c).status()
        self.assertEqual(state['day'], '2026-11-27')
        self.assertEqual(dec(state['day_open']), old_equity)
        self.assertFalse(state['day_latched'])


if __name__ == '__main__':
    unittest.main()
