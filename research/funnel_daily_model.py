"""Approved Funnel model session, isolated from accounts and formal Forward."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd

from core import p0_rq_input as rq
from research.research_store import ResearchStore
from research.ret120_execution_funnel import preview_size
from scripts.run_ret120_execution_funnel_v01 import C

STRATEGY = 'FUNNEL_MODEL_V1'
POLICY = {'strategy_id': STRATEGY, 'approved': '20261028',
          'stop': 'ENTRY_FROZEN_STOP_RAW_CLOSE_LE_THEN_NEXT_LEGAL_OPEN',
          'one_way_cost': '0.0025', 'money_rounding': 'CNY_CENT_ROUND_HALF_UP',
          'take_profit': None, 'trailing_stop': None, 'max_holding_sessions': None}


def money(value):
    return Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)


def cost(notional):
    return money(Decimal(str(notional)) * Decimal(POLICY['one_way_cost']))


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _read(path):
    import duckdb
    return duckdb.connect(str(path), read_only=True, config={'temp_directory': '',
        'autoinstall_known_extensions': 'false', 'autoload_known_extensions': 'false', 'threads': '1'})


def _aware(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError('FUNNEL_AWARE_FREEZE_REQUIRED')
    return stamp


def run_session(*, day, market_dates, output_db, histories, plans, identities, input_binding,
                parent_db=None, parent_day=None, parent_frozen_at=None, plan_frozen_at=None,
                initial_nav=1_000_000.0):
    day = pd.Timestamp(day).strftime('%Y%m%d')
    dates = [pd.Timestamp(d).strftime('%Y%m%d') for d in market_dates]
    if day not in dates or len(dates) != len(set(dates)) or dates != sorted(dates):
        raise ValueError('FUNNEL_APPROVED_SESSION_REQUIRED')
    actual = input_binding.get('actual_model_forward', False)
    if type(actual) is not bool:
        raise ValueError('FUNNEL_ACTUAL_FORWARD_FLAG_MUST_BE_BOOLEAN')
    if actual:
        now = pd.Timestamp(rq.clock()).tz_convert(rq.CST)
        if (day < '20261029' or now.strftime('%Y%m%d') != day
                or not (15, 30) <= (now.hour, now.minute) < (17, 15)):
            raise ValueError('FUNNEL_ACTUAL_FORWARD_REQUIRES_REAL_APPROVED_DAY_WINDOW')
    config = C()
    fingerprint = hashlib.sha256(_dump(dict(POLICY, risk=config['risk_preview'],
        caps=config['selection_caps'], next_open=config['next_open_policy'],
        initial_nav=float(money(initial_nav)))).encode()).hexdigest()
    binding = dict(day=day, input=input_binding, plans=plans, policy_hash=fingerprint)
    binding_hash = hashlib.sha256(_dump(binding).encode()).hexdigest()
    target = Path(output_db).resolve()
    if target.exists():
        with _read(target) as con:
            row = con.execute('SELECT binding_hash,result_json FROM funnel_model_sessions WHERE trade_date=?', [day]).fetchone()
        if not row or row[0] != binding_hash:
            raise ValueError('FUNNEL_EXISTING_SESSION_INPUT_CONFLICT')
        return dict(json.loads(row[1]), idempotent_reuse=True)
    if input_binding.get('model_research_authorized') is not True:
        raise ValueError('FUNNEL_MODEL_AUTHORIZATION_REQUIRED')
    if any(p.get('status') != 'PREPARE_MANUAL' for p in plans):
        raise ValueError('FUNNEL_ONLY_FROZEN_PREPARE_MANUAL_PLANS_ALLOWED')
    freeze = _aware(plan_frozen_at or rq.clock())
    if freeze < pd.Timestamp(day).tz_localize(rq.CST).replace(hour=15, minute=30):
        raise ValueError('FUNNEL_PLAN_BEFORE_COMPLETED_CLOSE')
    state = {'cash': float(money(initial_nav)), 'equity': float(money(initial_nav)),
             'initial_nav': float(money(initial_nav)), 'last_processed_date': None,
             'positions': [], 'plans': [], 'exit_plans': [], 'plan_frozen_at': None,
             'actual_model_forward': actual}
    if parent_db:
        if dates.index(day) == 0 or parent_day != dates[dates.index(day)-1]:
            raise ValueError('FUNNEL_ADJACENT_PARENT_REQUIRED')
        with _read(parent_db) as con:
            row = con.execute('SELECT policy_hash,state_json FROM forward_state WHERE strategy_id=?', [STRATEGY]).fetchone()
        if not row or row[0] != fingerprint:
            raise ValueError('FUNNEL_PARENT_POLICY_MISMATCH')
        state = json.loads(row[1])
        if state.get('actual_model_forward', False) is not actual:
            raise ValueError('FUNNEL_FORWARD_CANNOT_INHERIT_HISTORICAL_MODEL')
        if state['last_processed_date'] != parent_day:
            raise ValueError('FUNNEL_PARENT_CURSOR_MISMATCH')
        if any(p.get('signal_date') != parent_day for p in state['plans']):
            raise ValueError('FUNNEL_PARENT_PLANS_NOT_PREVIOUS_DAY')
        parent_freeze = _aware(parent_frozen_at)
        if parent_freeze >= pd.Timestamp(day).tz_localize(rq.CST).replace(hour=9, minute=30):
            raise ValueError('FUNNEL_PLAN_NOT_FROZEN_BEFORE_OPEN')
        before = rq.sha(parent_db)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(parent_db, target)
        if rq.sha(target) != before or rq.sha(parent_db) != before:
            raise ValueError('FUNNEL_PARENT_CHANGED_DURING_COPY')
    previous_equity = state['equity']
    cash = money(state['cash'])
    positions = {p['symbol']: dict(p) for p in state['positions']}
    pending_exits = {p['symbol']: dict(p) for p in state['exit_plans']}
    entries, decisions, exits, blocked, delayed = [], [], [], [], []
    bars = {}
    for symbol in sorted(set(positions) | {p['symbol'] for p in state['plans']}):
        frame = histories.get(symbol)
        bar = rq.execution_bar(frame, day)
        row = frame.loc[day]
        affected = bool(row.get('corporate_action_affected', False))
        if symbol in positions and abs(float(bar['factor'])-float(positions[symbol].get('entry_factor', bar['factor']))) > 1e-10:
            affected = True
        if affected and not bool(row.get('corporate_action_equivalence_verified', False)):
            raise ValueError('BLOCKED_CORPORATE_ACTION_EQUIVALENCE:' + symbol)
        bars[symbol] = bar
    # Entries spend only cash available at this open, before exit proceeds.
    open_mv = sum(money(p['shares'] * (bars[s].get('open') or p['last_price'])) for s, p in positions.items())
    opening_nav = float(cash + open_mv)
    for plan in state['plans']:
        symbol = plan['symbol']; bar = bars[symbol]
        reason = None
        detail = identities.get(symbol, {})
        special = detail.get('special_type', detail.get('VendorSpecialType'))
        if special not in ('Normal', 'ST', 'StarST'):
            reason = 'UNKNOWN_SECURITY_CLASSIFICATION'
        elif symbol in positions:
            reason = 'ALREADY_HELD'
        elif not bar['tradable']:
            reason = 'NO_REAL_TRADE_OR_SUSPENDED'
        elif bar.get('up_stop_price') is None:
            reason = 'EXACT_UP_LIMIT_UNKNOWN'
        elif bar['open'] >= float(bar['up_stop_price']) - bar['price_tick']/2:
            reason = 'OPEN_AT_UP_LIMIT'
        elif not float(plan['open_valid_low']) <= bar['open'] <= float(plan['open_valid_high']):
            reason = 'OPEN_OUTSIDE_FROZEN_ZONE'
        if reason:
            decisions.append({'symbol': symbol, 'decision': 'NOT_ENTERED', 'reason': reason})
            continue
        rule = bar.get('price_tick_rule', {})
        minimum = int(rule.get('minimum_order_quantity', plan['minimum_order_quantity']))
        step = int(rule.get('quantity_step', plan['quantity_step']))
        sized = preview_size(bar['open'], float(plan['stop_raw']), float(plan['adv20_amount']),
            opening_nav, plan['lane'], config['risk_preview'], minimum_order_quantity=minimum, quantity_step=step)
        shares = sized['preview_shares']
        for _ in range(3):
            if shares < minimum:
                break
            notional = money(Decimal(str(bar['open'])) * shares)
            fee = cost(notional)
            if notional + fee <= cash:
                break
            affordable = int(cash / (Decimal(str(bar['open'])) * Decimal('1.0025')))
            revised = minimum + ((affordable-minimum)//step)*step if affordable >= minimum else 0
            shares = min(shares-step, revised)
        if shares >= minimum and notional + fee > cash:
            shares = 0
        if shares < minimum:
            decisions.append({'symbol': symbol, 'decision': 'NOT_ENTERED',
                              'reason': 'SKIP_MIN_ORDER_RISK' if sized['preview_shares'] < minimum else 'INSUFFICIENT_OPEN_CASH'})
            continue
        cash -= notional + fee
        position = dict(symbol=symbol, signal_date=parent_day, entry_date=day, entry_price=bar['open'],
            shares=shares, entry_notional=float(notional), entry_fee=float(fee), last_price=bar['open'],
            stop_raw=float(plan['stop_raw']), entry_factor=bar['factor'], lane=plan['lane'])
        positions[symbol] = position
        entries.append(dict(position, status='MODEL_ENTRY_NOT_REAL_ACCOUNT'))
        decisions.append({'symbol': symbol, 'decision': 'ENTERED', 'reason': 'FROZEN_ZONE_RISK_CASH_AND_RULES_PASS', 'shares': shares})
    for symbol, exit_plan in list(pending_exits.items()):
        position = positions[symbol]; bar = bars[symbol]
        if position['entry_date'] >= day or exit_plan['trigger_date'] >= day:
            delayed.append({'symbol': symbol, 'reason': 'T_PLUS_ONE_OR_NEXT_SESSION_REQUIRED'})
        elif not bar['tradable']:
            delayed.append({'symbol': symbol, 'reason': 'NO_REAL_TRADE_OR_SUSPENDED'})
        elif bar.get('down_stop_price') is None:
            raise ValueError('FUNNEL_EXACT_EXIT_DOWN_LIMIT_UNKNOWN:' + symbol)
        elif bar['open'] <= float(bar['down_stop_price']) + bar['price_tick']/2:
            delayed.append({'symbol': symbol, 'reason': 'OPEN_AT_DOWN_LIMIT'})
        else:
            proceeds = money(Decimal(str(bar['open'])) * position['shares']); fee = cost(proceeds)
            cash += proceeds-fee
            pnl = money(proceeds-fee-money(position['entry_notional'])-money(position['entry_fee']))
            exits.append(dict(position, exit_date=day, exit_price=bar['open'], exit_fee=float(fee),
                              net_pnl=float(pnl), reason='FROZEN_STOP_NEXT_LEGAL_OPEN'))
            del positions[symbol]; del pending_exits[symbol]
    for symbol, position in positions.items():
        bar = bars[symbol]
        if bar['tradable']:
            position['last_price'] = bar['close']
            position['valuation_status'] = 'EXACT_REAL_CLOSE'
            if bar['close'] <= position['stop_raw'] and symbol not in pending_exits:
                pending_exits[symbol] = {'symbol': symbol, 'trigger_date': day,
                    'reason': 'VALID_CLOSE_LE_FROZEN_STOP', 'stop_raw': position['stop_raw']}
        else:
            position['valuation_status'] = 'CARRIED_LAST_REAL_PRICE_NO_TRADE'
    market_value = sum((money(p['shares']*p['last_price']) for p in positions.values()), Decimal('0'))
    equity = money(cash + market_value)
    new_plans = [dict(p, signal_date=day, frozen_at=freeze.isoformat(),
                      model_only=True, manual_approval_required=True) for p in plans]
    state.update(cash=float(cash), equity=float(equity), last_processed_date=day,
                 positions=list(positions.values()), plans=new_plans, exit_plans=list(pending_exits.values()),
                 plan_frozen_at=freeze.isoformat())
    result = {'status': 'FUNNEL_MODEL_SESSION_COMPLETE', 'strategy_id': STRATEGY, 'policy_hash': fingerprint,
        'business_date': day, 'state': state, 'entries': entries, 'entry_decisions': decisions,
        'positions': state['positions'], 'exits': exits, 'exit_plans': state['exit_plans'], 'plans': new_plans,
        'blocked': blocked, 'delayed': delayed, 'exit_evaluation_count': len(bars),
        'settlement': {'status': 'CALCULATED', 'cash': float(cash), 'equity': float(equity),
            'gross_market_value': float(market_value), 'day_pnl': float(equity-money(previous_equity)),
            'total_return': float(equity/Decimal(str(state['initial_nav']))-1)},
        'stage_status': {'entries': 'CALCULATED' if entries else 'CALCULATED_ZERO',
                         'exits': 'CALCULATED' if exits else 'CALCULATED_ZERO', 'settlement': 'CALCULATED'},
        'input_binding': input_binding, 'forward_acceptance': False,
        'mode': 'ACTUAL_MODEL_FORWARD' if actual else 'ISOLATED_FUNNEL_MODEL_NOT_FORWARD',
        'real_account': 'UNKNOWN_NOT_QUERIED'}
    result['forward_acceptance'] = actual
    with ResearchStore(target) as store:
        store.con.execute('BEGIN TRANSACTION')
        try:
            store.con.execute('CREATE TABLE IF NOT EXISTS funnel_model_sessions(trade_date VARCHAR PRIMARY KEY,binding_hash VARCHAR,result_json VARCHAR)')
            store.con.execute('DELETE FROM forward_positions WHERE strategy_id=?', [STRATEGY])
            for p in positions.values():
                store.con.execute('INSERT INTO forward_positions VALUES (?,?,?,?,?,?,?,?,?,?)',
                    [STRATEGY,'FUNNEL',p['symbol'],p['signal_date'],p['entry_date'],p['entry_price'],
                     p['shares'],p['entry_notional'],p['entry_fee'],p['last_price']])
            for trade in exits:
                store.con.execute('INSERT INTO forward_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    [STRATEGY,'FUNNEL',trade['symbol'],trade['signal_date'],trade['entry_date'],day,
                     trade['entry_price'],trade['exit_price'],trade['entry_notional'],trade['net_pnl'],
                     trade['net_pnl']/trade['entry_notional'],.005])
            store.con.execute('INSERT INTO forward_equity VALUES (?,?,?,?,?,?,?,?)',
                [STRATEGY,'FUNNEL',day,float(equity),float(cash),float(market_value),
                 float(market_value/equity) if equity else 0.,len(positions)])
            store.save_state(STRATEGY, fingerprint, state)
            store.con.execute('INSERT INTO funnel_model_sessions VALUES (?,?,?)', [day,binding_hash,_dump(result)])
            store.con.execute('COMMIT')
        except BaseException:
            store.con.execute('ROLLBACK')
            raise
    return result
