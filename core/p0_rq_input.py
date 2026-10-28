"""Fixed RQData input for P0. No download, broker, automatic fallback or writes.

RQ_PRE / RQ_POST are named RQ representations, never implicit QMT aliases.
Strategy representations require a separate reviewed compatibility contract.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import re

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'config/ops/p0_rq_input_v01.json'
CST = timezone(timedelta(hours=8))


class InputBlocked(RuntimeError):
    pass


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def clock():
    return datetime.now(CST)


def require_trading_date(value):
    d = pd.Timestamp(value)
    c = json.loads((ROOT / 'config/ops/cn_exchange_calendar_2026.json').read_text(encoding='utf-8'))
    key = d.strftime('%Y%m%d')
    if (not c['valid_from'] <= key <= c['valid_to'] or d.weekday() >= 5
            or any(r['start'] <= key <= r['end'] for r in c['closed_ranges'])):
        raise InputBlocked('RQ_NOT_APPROVED_EXCHANGE_SESSION')


def profile():
    if not PROFILE.exists():
        return {'enabled': False}
    p = json.loads(PROFILE.read_text(encoding='utf-8'))
    if type(p.get('enabled')) is not bool:
        raise InputBlocked('RQ_PROFILE_INVALID')
    return p


def enabled():
    return profile()['enabled']


def rq_id(symbol):
    if not re.fullmatch(r'\d{6}\.(SH|SZ)', symbol):
        raise InputBlocked('INVALID_SECURITY_IDENTITY')
    return symbol.replace('.SH', '.XSHG').replace('.SZ', '.XSHE')


def qmt_id(symbol):
    if not re.fullmatch(r'\d{6}\.(XSHG|XSHE)', symbol):
        raise InputBlocked('INVALID_RQ_SECURITY_IDENTITY')
    return symbol.replace('.XSHG', '.SH').replace('.XSHE', '.SZ')


def open_inputs(asof=None, *, scope='P0_COMPLETE'):
    if scope not in ('P0_COMPLETE', 'DAILY_SIGNALS_V1'):
        raise InputBlocked('RQ_INPUT_SCOPE_INVALID')
    p = profile()
    if not p['enabled']:
        return None
    target = pd.Timestamp(asof or clock().date()).date().isoformat()
    # Exact-date, complete version only. Never use the nearest older snapshot.
    candidates = []
    versions = 'research_versions' if scope == 'DAILY_SIGNALS_V1' else 'versions'
    for f in Path(p['asset_root']).glob(versions+'/*/source_metadata.json'):
        meta = json.loads(f.read_text(encoding='utf-8'))
        if meta.get('asof') == target and pd.Timestamp(meta['availability']) <= pd.Timestamp(clock()):
            candidates.append((meta['availability'], f.parent / 'manifest.json'))
    if not candidates:
        raise InputBlocked('RQ_EXACT_DATE_VERSION_MISSING:' + target)
    if scope == 'DAILY_SIGNALS_V1':
        return DailyInput(max(candidates)[1], p, scope=scope)
    return DailyInput(max(candidates)[1], p)


def normalize_actions(dividends, splits, rights, info, observed_at):
    """Normalize existing RQ records; split already includes bonus + transfer.

    Date-only announcements are conservatively available at end of that day.
    Observation stays the real capture time, not the historical announcement.
    Ambiguous rights terms require evidence instead of choosing a matching ratio.
    """
    events = {}
    def event(obid, day):
        key = (obid, pd.Timestamp(day).normalize())
        return events.setdefault(key, dict(order_book_id=obid, ex_date=key[1],
            cash_per_share=0.0, share_multiplier=1.0, rights_per_share=0.0,
            rights_price=0.0, available_at=None, observed_at=pd.Timestamp(observed_at).isoformat()))
    def available(e, day):
        t = pd.Timestamp(day)
        if pd.isna(t):
            raise InputBlocked('ACTION_PUBLICATION_DATE_MISSING')
        t = t.tz_localize(CST) if t.tzinfo is None else t.tz_convert(CST)
        t = t.normalize() + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        e['available_at'] = max(t, pd.Timestamp(e['available_at'])).isoformat() if e['available_at'] else t.isoformat()
    for r in dividends.itertuples(index=False):
        if pd.isna(r.ex_dividend_date):
            continue
        if not np.isfinite([r.round_lot, r.dividend_cash_before_tax]).all() or r.round_lot <= 0:
            raise InputBlocked('ACTION_CASH_UNIT_INVALID')
        e = event(r.order_book_id, r.ex_dividend_date)
        e['cash_per_share'] += r.dividend_cash_before_tax / r.round_lot
        available(e, r.declaration_announcement_date)
    if not splits.empty and splits.duplicated(['order_book_id', 'ex_dividend_date']).any():
        raise InputBlocked('ACTION_SPLIT_VERSION_AMBIGUOUS')
    for r in splits.itertuples(index=False):
        e = event(r.order_book_id, r.ex_dividend_date)
        if not np.isfinite([r.split_coefficient_to, r.split_coefficient_from]).all() or min(r.split_coefficient_to, r.split_coefficient_from) <= 0:
            raise InputBlocked('ACTION_SPLIT_UNIT_INVALID')
        e['share_multiplier'] = r.split_coefficient_to / r.split_coefficient_from
        # A record date is not a publication time. Require the matching info row.
    for r in info.itertuples(index=False):
        if pd.isna(r.ex_dividend_date):
            continue
        key = (r.order_book_id, pd.Timestamp(r.ex_dividend_date).normalize())
        if key not in events:
            continue
        e = events[key]
        available(e, r.info_date)
        if pd.notna(r.rice_create_tm):
            t = pd.Timestamp(r.rice_create_tm).tz_localize(CST)
            e['available_at'] = max(pd.Timestamp(e['available_at']), t).isoformat()
    for r in rights.itertuples(index=False):
        if pd.isna(r.ex_right_date) or r.stock_type != 'a_share':
            continue
        if not np.isfinite([r.proportion, r.allotted_proportion, r.allotment_price]).all() or abs(r.proportion - r.allotted_proportion) > 1e-12:
            raise InputBlocked('ACTION_RIGHTS_RATIO_REQUIRES_NATIVE_EVIDENCE')
        e = event(r.order_book_id, r.ex_right_date)
        if e['rights_per_share']:
            raise InputBlocked('ACTION_RIGHTS_VERSION_AMBIGUOUS')
        e.update(rights_per_share=r.allotted_proportion, rights_price=r.allotment_price)
        available(e, r.declaration_announcement_date)
    if any(e['available_at'] is None for e in events.values()):
        raise InputBlocked('ACTION_PUBLICATION_EVIDENCE_MISSING')
    return pd.DataFrame(events.values())


def affine_front(frame, actions, asof, decision):
    """QMT affine OHLC only. Inputs are versioned per-share action terms.

    Same-day cash/split/rights must be one combined event. No price rounding:
    QMT history in the retained samples preserves sub-tick adjusted prices.
    Publication/observation times are checked against the actual decision,
    never inferred from an ex-date or record date.
    """
    x = frame.copy()
    end = pd.Timestamp(asof).normalize()
    now = pd.Timestamp(decision)
    if now.tzinfo is None:
        raise InputBlocked('ACTION_DECISION_TIME_INVALID')
    if actions.empty:
        return x
    a = actions.copy()
    a['ex_date'] = pd.to_datetime(a.ex_date)
    a = a.loc[(a.ex_date > pd.to_datetime(x.date).min()) & (a.ex_date <= end)]
    if a.duplicated('ex_date').any():
        raise InputBlocked('ACTION_AMBIGUOUS_SAME_DATE')
    for r in a.sort_values('ex_date').itertuples(index=False):
        for value in (r.available_at, r.observed_at):
            t = pd.Timestamp(value)
            if pd.isna(t) or t.tzinfo is None or t > now:
                raise InputBlocked('ACTION_NOT_AVAILABLE_AT_DECISION')
        terms = [r.cash_per_share, r.share_multiplier, r.rights_per_share, r.rights_price]
        if (not np.isfinite(terms).all() or r.share_multiplier <= 0
                or min(r.cash_per_share, r.rights_per_share, r.rights_price) < 0):
            raise InputBlocked('ACTION_TERMS_INVALID')
        before = pd.to_datetime(x.date) < r.ex_date
        for field in ('open', 'high', 'low', 'close'):
            x.loc[before, field] = (x.loc[before, field] - r.cash_per_share
                + r.rights_per_share * r.rights_price) / (r.share_multiplier + r.rights_per_share)
    return x


def real_observation(row, decision):
    """Trade evidence, not a liquidity filter. A padded quote is never a fill."""
    status = row.get('status_suspended')
    if (isinstance(status, (bool, np.bool_)) and status) or bool(row.get('is_suspended', False)):
        return False
    # Historical features may use observed real trade counts without inventing
    # an old instrument-status snapshot. Execution still requires explicit status.
    names = ['bar_available_at']
    if isinstance(status, (bool, np.bool_)):
        names.append('status_available_at')
    for name in names:
        t = pd.Timestamp(row.get(name))
        if pd.isna(t) or t.tzinfo is None or t > pd.Timestamp(decision):
            return False
    vals = [row.get('volume'), row.get('num_trades')]
    return all(v is not None and pd.notna(v) and np.isfinite(float(v)) and float(v) > 0 for v in vals)


def frozen_ret120_prices(frame):
    """Original frozen load_panel economics, with an arbitrary constant level.

    RQ_POST rounded cumulative factors are deliberately not used here. Ratios
    reproduce the original cumulative close/prev_close definition. No future row
    is needed. A broken factor inside the lookback remains invalid.
    """
    x = frame.copy()
    if 'prev_close' not in x:
        raise InputBlocked('RET120_FROZEN_PREV_CLOSE_MISSING')
    valid = np.isfinite(x.close) & np.isfinite(x.prev_close) & (x.close > 0) & (x.prev_close > 0)
    daily = np.where(valid, x.close / x.prev_close, np.nan)
    logs = pd.Series(np.where(daily > 0, np.log(daily), np.nan), index=x.index)
    adjusted_close = np.exp(logs.fillna(0.0).cumsum())
    factor = adjusted_close / x.raw_close
    for field in ('open', 'high', 'low', 'close'):
        if 'raw_' + field in x:
            x[field] = x['raw_' + field] * factor
    x['factor_valid'] = valid
    return x


def feature_histories(histories, decision=None):
    out = {}
    for symbol, frame in histories.items():
        if frame.attrs.get('source') == 'RQDATA':
            frame = frame.loc[[real_observation(row, decision or clock()) for _, row in frame.iterrows()]]
            if 'factor_valid' in frame and len(frame):
                # Same 121-factor guard as the frozen signal pipeline; filtering
                # non-trades must not hide a bad factor within the return window.
                bad = (~frame.factor_valid).rolling(121, min_periods=121).sum()
                if pd.isna(bad.iloc[-1]) or bad.iloc[-1] != 0:
                    frame = frame.iloc[:0]
        out[symbol] = frame
    return out


def execution_bar(frame, target, decision=None):
    """Exact-session raw execution inputs; adjusted prices remain research units."""
    if frame is not None and (frame.attrs.get('scope') == 'DAILY_SIGNALS_V1'
            or frame.attrs.get('qualification_scope') == 'DAILY_SIGNALS_V1'):
        raise InputBlocked('EXECUTION_DAILY_SIGNALS_SCOPE_FORBIDDEN')
    day = pd.Timestamp(target).strftime('%Y%m%d')
    if frame is None or day not in frame.index:
        raise InputBlocked('EXECUTION_EXACT_BAR_MISSING')
    r = frame.loc[day]
    if isinstance(r, pd.DataFrame):
        raise InputBlocked('EXECUTION_DUPLICATE_BAR')
    if not isinstance(r.get('status_suspended'), (bool, np.bool_)):
        raise InputBlocked('EXECUTION_STATUS_UNKNOWN')
    if ('factor_valid' in r and not r['factor_valid']) or not np.isfinite(float(r['research_factor'])) or r['research_factor'] <= 0:
        raise InputBlocked('EXECUTION_RESEARCH_FACTOR_INVALID')
    tick = r.get('price_tick')
    tick_time = pd.Timestamp(r.get('tick_available_at'))
    now = pd.Timestamp(decision or clock())
    for name in ('status_available_at', 'bar_available_at'):
        observed = pd.Timestamp(r.get(name))
        if pd.isna(observed) or observed.tzinfo is None or observed > now:
            raise InputBlocked('EXECUTION_EVIDENCE_NOT_AVAILABLE')
    if (tick is None or pd.isna(tick) or not np.isfinite(float(tick)) or float(tick) <= 0
            or pd.isna(tick_time) or tick_time.tzinfo is None or tick_time > now
            or tick_time.tz_convert(CST).date() != pd.Timestamp(target).date()):
        raise InputBlocked('EXECUTION_EXACT_DATE_TICK_MISSING')
    tradable = real_observation(r, now)
    out = {field: (float(r['raw_' + field]) if tradable else None)
           for field in ('open', 'high', 'low', 'close')}
    out.update(up_stop_price=r.get('limit_up'), down_stop_price=r.get('limit_down'),
               price_tick=float(tick), instrument_status=int(r['status_suspended']),
               prev_close=r.get('prev_close'), tradable=tradable,
               factor=float(r['research_factor']), source=frame.attrs.get('source'),
               version=frame.attrs.get('version'), available_at=r.get('bar_available_at'))
    return out


class DailyInput:
    def __init__(self, manifest_path, config, now=None, *, scope='P0_COMPLETE'):
        if scope not in ('P0_COMPLETE', 'DAILY_SIGNALS_V1'):
            raise InputBlocked('RQ_INPUT_SCOPE_INVALID')
        self.root = Path(manifest_path).parent.resolve()
        self.config = config
        self.now = pd.Timestamp(now or clock())
        marker = json.loads((self.root / 'COMPLETE.json').read_text(encoding='utf-8'))
        if marker.get('manifest_sha256') != sha(manifest_path):
            raise InputBlocked('RQ_MANIFEST_HASH_MISMATCH')
        m = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
        inventory = {x['path']: x for x in m['files']}
        if len(inventory) != len(m['files']) or 'source_metadata.json' not in inventory:
            raise InputBlocked('RQ_INVENTORY_INVALID')
        for name, item in inventory.items():
            path = (self.root / name).resolve()
            if not path.is_relative_to(self.root) or sha(path) != item['sha256']:
                raise InputBlocked('RQ_FILE_HASH_MISMATCH')
        self.meta = json.loads((self.root / 'source_metadata.json').read_text(encoding='utf-8'))
        qualification = self.meta.get('qualification_scope', 'P0_COMPLETE')
        if qualification == 'DAILY_SIGNALS_V1':
            if (scope != 'DAILY_SIGNALS_V1'
                    or self.meta.get('quality_status') != 'DAILY_SIGNALS_PASS_V1'
                    or self.meta.get('engineering_only') is not True
                    or self.meta.get('full_p0_pass') is not False
                    or self.meta.get('execution_qualified') is not False):
                raise InputBlocked('RQ_DAILY_SIGNALS_SCOPE_OR_QUALIFICATION_INVALID')
        elif qualification != 'P0_COMPLETE' or scope != 'P0_COMPLETE':
            raise InputBlocked('RQ_INPUT_QUALIFICATION_SCOPE_MISMATCH')
        self.scope = scope
        self.db = self.root / self.meta['database']
        if (self.meta['database'] not in inventory or self.meta['source'] != 'RQDATA'
                or self.meta['raw_adjustment'] != 'none'
                or sha(self.db) != self.meta['database_sha256']):
            raise InputBlocked('RQ_SOURCE_BINDING_INVALID')
        if self.now.tzinfo is None or pd.Timestamp(self.meta['availability']) > self.now:
            raise InputBlocked('RQ_NOT_AVAILABLE_AT_DECISION')
        business_date = pd.Timestamp(self.meta['asof']).date()
        if (business_date > self.now.date() or (business_date == self.now.date()
                and self.now < self.now.normalize() + pd.Timedelta(hours=15, minutes=30))):
            raise InputBlocked('RQ_COMPLETE_CLOSE_NOT_YET_AVAILABLE')

    def require_contract(self, adjustment):
        if adjustment in ('none', 'RQ_PRE', 'RQ_POST'):
            return
        if adjustment == 'front':
            # QMT front is affine corporate-action adjustment, not RQ pre ratio.
            # The sealed v03 asset has cumulative factors but no complete native-
            # compatible action conversion. A config flag cannot alias the two.
            c = self.config.get('strategy_contracts', {}).get('front', {})
            if c.get('representation') != 'QMT_FRONT_AFFINE':
                raise InputBlocked('RQ_FRONT_AFFINE_ACTION_CONVERSION_REQUIRED')
        c = self.config.get('strategy_contracts', {}).get(adjustment, {})
        if (c.get('accepted') is not True or not c.get('evidence_sha256')
                or not c.get('evidence_file') or sha(c['evidence_file']) != c['evidence_sha256']
                or c.get('representation') != ('QMT_FRONT_AFFINE' if adjustment == 'front' else 'RET120_FROZEN_CLOSE_PRE_CLOSE')
                or c.get('execution_price_basis_accepted') is not True
                or c.get('suspension_semantics_accepted') is not True):
            raise InputBlocked('RQ_STRATEGY_CONTRACT_UNVERIFIED:' + adjustment)

    def lineage(self, adjustment):
        actual = self.config.get('strategy_contracts', {}).get(adjustment, {}).get('representation', adjustment)
        result = {'source': 'RQDATA', 'adjustment': actual, 'requested_adjustment': adjustment,
                'unit': self.meta['volume_unit'], 'asof': self.meta['asof'],
                'availability': self.meta['availability'], 'version': self.meta['version']}
        if self.meta.get('qualification_scope') == 'DAILY_SIGNALS_V1':
            result.update(scope='DAILY_SIGNALS_V1', qualification_scope='DAILY_SIGNALS_V1')
        return result

    def benchmark_history(self, symbol, dates):
        """Read exact native index observations from this seal, without SDK fallback."""
        requested = [pd.Timestamp(d).date() for d in dates]
        binding = self.meta.get('benchmark', {})
        if (symbol != '000905.SH' or not requested or len(set(requested)) != len(requested)
                or max(requested).isoformat() > self.meta['asof']
                or binding.get('table') != 'benchmark' or binding.get('source') != 'RQDATA'
                or binding.get('price_unit') != 'INDEX_POINTS'):
            raise InputBlocked('RQ_BENCHMARK_BINDING_MISSING_OR_WRONG_REQUEST')
        with duckdb.connect(str(self.db), read_only=True, config={'temp_directory': ''}) as con:
            frame = con.execute('''SELECT * FROM benchmark WHERE order_book_id=?
                AND CAST(date AS DATE) IN (SELECT unnest(?)) ORDER BY date''', [rq_id(symbol), requested]).df()
        if (len(frame) != len(requested) or frame.date.duplicated().any()
                or set(pd.to_datetime(frame.date).dt.date) != set(requested)
                or not frame.source.eq('RQDATA').all() or not frame.price_unit.eq('INDEX_POINTS').all()):
            raise InputBlocked('RQ_BENCHMARK_EXACT_ROWS_OR_PROVENANCE_MISSING')
        bindings = binding.get('date_bindings') or {d: dict(
            source_database_sha256=binding.get('source_database_sha256'), observed_at=binding.get('observed_at'))
            for d in binding.get('dates', [])}
        for row in frame.itertuples():
            expected = bindings.get(pd.Timestamp(row.date).strftime('%Y%m%d'), {})
            if (not expected or row.source_database_sha256 != expected.get('source_database_sha256')
                    or row.observed_at != expected.get('observed_at')):
                raise InputBlocked('RQ_BENCHMARK_EXACT_ROWS_OR_PROVENANCE_MISSING')
        values = frame[['open', 'high', 'low', 'close', 'prev_close']].to_numpy(dtype=float)
        if not np.isfinite(values).all() or (values <= 0).any():
            raise InputBlocked('RQ_BENCHMARK_VALUES_INVALID')
        frame.index = pd.to_datetime(frame.date).dt.strftime('%Y%m%d')
        frame.attrs.update(source='RQDATA', price_unit='INDEX_POINTS', version=self.meta['version'],
                           date_bindings={d.strftime('%Y%m%d'): bindings[d.strftime('%Y%m%d')] for d in requested})
        if self.meta.get('qualification_scope') == 'DAILY_SIGNALS_V1':
            frame.attrs.update(scope='DAILY_SIGNALS_V1', qualification_scope='DAILY_SIGNALS_V1')
        return frame

    def histories(self, symbols, asof, bars, adjustment='none', min_bars=1, no_fill=False,
                  allow_incomplete=False):
        self.require_contract(adjustment)
        target = pd.Timestamp(asof).date()
        require_trading_date(target)
        if target.isoformat() != self.meta['asof']:
            raise InputBlocked('RQ_STALE_OR_WRONG_BUSINESS_DATE')
        if type(bars) is not int or type(min_bars) is not int or bars < min_bars or min_bars < 1:
            raise InputBlocked('RQ_HISTORY_BUDGET_INVALID')
        symbols = list(dict.fromkeys(symbols))
        ids = [rq_id(s) for s in symbols]
        if not ids:
            return {}
        representation = adjustment
        if adjustment in ('front', 'back_ratio'):
            representation = self.config['strategy_contracts'][adjustment]['representation']
        with duckdb.connect(str(self.db), read_only=True, config={'temp_directory': ''}) as con:
            x = con.execute('''SELECT b.*, coalesce(f.ex_cum_factor, 1.0) AS factor
                FROM raw_daily b ASOF LEFT JOIN factors f
                ON b.order_book_id=f.order_book_id AND b.date>=f.ex_date
                WHERE b.order_book_id IN (SELECT unnest(?)) AND b.date<=?
                ORDER BY b.order_book_id,b.date''', [ids, target]).df()
            suspended = con.execute('SELECT order_book_id,date FROM suspension WHERE is_suspended IS TRUE').df()
            tables = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
            status = con.execute('SELECT * FROM execution_status').df() if 'execution_status' in tables else pd.DataFrame()
            actions = con.execute('SELECT * FROM corporate_actions').df() if 'corporate_actions' in tables else pd.DataFrame()
        if x.duplicated(['order_book_id', 'date']).any():
            raise InputBlocked('RQ_DUPLICATE_BAR_KEY')
        flags = set(zip(suspended.order_book_id, pd.to_datetime(suspended.date)))
        result = {}
        for symbol, obid in zip(symbols, ids):
            g = x.loc[x.order_book_id.eq(obid)].copy()
            if g.empty or g.date.max().date() != target:
                if allow_incomplete:
                    continue
                raise InputBlocked('RQ_EXACT_T_BAR_MISSING:' + symbol)
            fields = ['open', 'high', 'low', 'close', 'volume', 'total_turnover', 'factor']
            if not np.isfinite(g[fields].to_numpy()).all() or (g.factor <= 0).any():
                raise InputBlocked('RQ_INVALID_VALUES')
            if (g[['open', 'high', 'low', 'close']] <= 0).any().any() or (g.volume < 0).any():
                raise InputBlocked('RQ_INVALID_OHLC_VOLUME')
            if ((g.high < g[['open', 'close', 'low']].max(axis=1)) |
                    (g.low > g[['open', 'close', 'high']].min(axis=1))).any():
                raise InputBlocked('RQ_INVALID_OHLC_RANGE')
            g['is_suspended'] = [(obid, pd.Timestamp(d)) in flags for d in g.date]
            for field in ('open', 'high', 'low', 'close'):
                g['raw_' + field] = g[field]
            if not status.empty:
                ss = status.loc[status.order_book_id.eq(obid)].drop(columns='order_book_id')
                if ss.duplicated('date').any():
                    raise InputBlocked('RQ_DUPLICATE_EXECUTION_STATUS')
                g = g.merge(ss, on='date', how='left', validate='one_to_one')
            g['bar_available_at'] = self.meta['availability']
            factor = g.factor if representation in ('RQ_PRE', 'RQ_POST') else 1.0
            if representation == 'RQ_PRE':
                factor = factor / float(g.iloc[-1].factor)
            for field in ('open', 'high', 'low', 'close'):
                g[field] = g[field] * factor
            if representation == 'QMT_FRONT_AFFINE':
                if min_bars > 120:
                    raise InputBlocked('DEEP_FRONT_SCOPE_LIMIT_120')
                g = g.tail(min(bars, 120)).copy()
                if (self.meta.get('corporate_action_coverage_complete') is not True
                        or obid not in self.meta.get('corporate_action_symbols', [])
                        or pd.Timestamp(self.meta.get('corporate_action_coverage_end')).date() != target
                        or pd.isna(pd.Timestamp(self.meta.get('corporate_action_coverage_start')))
                        or pd.Timestamp(self.meta['corporate_action_coverage_start']) > g.date.min()):
                    raise InputBlocked('RQ_FRONT_ACTION_COVERAGE_MISSING')
                symbol_actions = actions.loc[actions.order_book_id.eq(obid)] if not actions.empty else actions
                g = affine_front(g, symbol_actions, target, self.now)
            if representation == 'RET120_FROZEN_CLOSE_PRE_CLOSE':
                g = frozen_ret120_prices(g)
            g['research_factor'] = g.close / g.raw_close
            # Raw volume and explicit suspension flag are preserved. No fake executions.
            if no_fill:
                if self.config.get('no_fill_semantics_accepted') is not True:
                    raise InputBlocked('RQ_NO_FILL_SEMANTICS_UNVERIFIED')
                # Counts/status establish a real observation. Do not introduce
                # liquidity minima or change the frozen prior ranking filters.
                g = g.loc[[real_observation(row, self.now) for _, row in g.iterrows()]]
                if 'factor_valid' in g and (len(g) < 121 or not g.tail(121).factor_valid.all()):
                    if allow_incomplete:
                        continue
                    raise InputBlocked('RET120_FROZEN_FACTOR_WINDOW_INVALID')
            g = g.tail(bars).rename(columns={'total_turnover': 'amount'})
            if len(g) < min_bars or g.empty or g.date.max().date() != target:
                if allow_incomplete:
                    continue
                raise InputBlocked('RQ_REQUIRED_HISTORY_NOT_MET:' + symbol)
            g.index = g.date.dt.strftime('%Y%m%d')
            g.attrs.update(self.lineage(adjustment))
            result[symbol] = g
        return result


def validate_preopen_membership(frame, business_date, decision=None):
    required = {'snapshot_date', 'effective_date', 'observed_at', 'provider_created_at',
                'index_code', 'symbol', 'source'}
    if not required.issubset(frame.columns) or frame.empty:
        raise InputBlocked('RQ_PIT_SCHEMA_MISSING')
    d = pd.Timestamp(business_date).date()
    require_trading_date(d)
    decision = pd.Timestamp(decision or clock())
    if decision.tzinfo is None:
        raise InputBlocked('RQ_PIT_DECISION_TIME_INVALID')
    obs = pd.to_datetime(frame.observed_at, utc=True).dt.tz_convert(CST)
    created = pd.to_datetime(frame.provider_created_at, utc=True).dt.tz_convert(CST)
    if (not frame.source.eq('RQDATA_COMPONENTS_PROVIDER').all()
            or not pd.to_datetime(frame.snapshot_date).dt.date.eq(d).all()
            or not pd.to_datetime(frame.effective_date).dt.date.eq(d).all()
            or not obs.dt.date.eq(d).all() or (obs > decision).any()
            or (obs >= pd.Timestamp(str(d) + ' 09:30:00', tz=CST)).any()
            or created.isna().any() or obs.isna().any() or (created > obs).any()
            or frame.duplicated(['index_code', 'symbol']).any()):
        raise InputBlocked('RQ_PIT_ASOF_INVALID')
    sets = {}
    for code, count in [('000300.SH', 300), ('000905.SH', 500), ('000852.SH', 1000)]:
        members = set(frame.loc[frame.index_code.eq(code), 'symbol'])
        for symbol in members:
            rq_id(symbol)
        if len(members) != count:
            raise InputBlocked('RQ_PIT_COUNT_INVALID')
        sets[code] = members
    if len(set().union(*sets.values())) != 1800:
        raise InputBlocked('RQ_PIT_INDEX_OVERLAP')
    return sets


def preopen_file(pit_root, business_date):
    d = pd.Timestamp(business_date).strftime('%Y%m%d')
    paths = sorted((Path(pit_root) / 'raw' / d / 'preopen').glob('*/index_membership_rqdata.parquet'))
    if not paths:
        raise InputBlocked('RQ_EXACT_DATE_PREOPEN_PIT_MISSING')
    path = paths[-1]
    validate_preopen_membership(pd.read_parquet(path), business_date)
    return path


def current_universe():
    p = profile()
    path = preopen_file(p['pit_root'], clock().date())
    sets = validate_preopen_membership(pd.read_parquet(path), clock().date())
    return {'ok': True, 'members': sorted(sets['000905.SH']),
            'source': 'RQDATA_COMPONENTS_PROVIDER|preopen_sha256=' + sha(path)}


def strategy_histories(xtdata, symbols, bars, adjustment, min_bars):
    inputs = open_inputs()
    inputs.require_contract(adjustment)
    stocks = [s for s in symbols if s != '000905.SH']
    # RET120 retains its existing >=400/500 feature gate and strict busy-position
    # checks in the caller. Short/non-current optional candidates stay invalid;
    # do not silently raise the frozen coverage gate to 500/500 here.
    out = inputs.histories(stocks, clock().date(), bars, adjustment, min_bars,
                           allow_incomplete=(adjustment == 'back_ratio'))
    if '000905.SH' in symbols:
        # Explicit separate source for the benchmark, never a stock-history fallback.
        # Existing Full-QMT local read. Do not refresh or download the index.
        from research.watchlist_adapter import AdapterConfig, XtDataWatchlistAdapter
        frames = XtDataWatchlistAdapter(xtdata, AdapterConfig(
            sector='', bars=bars, dividend_type=adjustment)).history(['000905.SH'])
        frame = frames.get('000905.SH')
        if (frame is None or len(frame) < min_bars
                or max(pd.to_datetime(frame.index)).date() != clock().date()):
            raise InputBlocked('FULL_QMT_LOCAL_INDEX_NOT_CURRENT')
        frame.attrs.update(source='FULL_QMT_LOCAL', adjustment=adjustment,
                           unit='QMT_VENDOR_ORIGINAL', asof=inputs.meta['asof'],
                           availability=clock().isoformat(), version='PINNED_FULL_QMT_RUNTIME')
        out['000905.SH'] = frame
    return out


def source_label(adjustment):
    if not enabled():
        from core.qmt_runtime import is_full
        return 'FULL_QMT_FORWARD_OBSERVED' if is_full() else 'MINIQMT_FORWARD_OBSERVED'
    return 'RQ_STOCKS_PLUS_FULL_QMT_LOCAL_INDEX|' + json.dumps(
        open_inputs().lineage(adjustment), sort_keys=True)


def prior_close(symbols, signal_date):
    inputs = open_inputs(signal_date)
    frames = inputs.histories(symbols, signal_date, 320, 'back_ratio', 121,
                              no_fill=True, allow_incomplete=True)
    rows = [{'trade_date': pd.Timestamp(d), 'symbol': s, 'close': float(r.close)}
            for s, f in frames.items() for d, r in f.iterrows()]
    out = pd.DataFrame(rows, columns=['trade_date', 'symbol', 'close'])
    out.attrs.update(inputs.lineage('back_ratio'))
    return out


def full_market(signal_date):
    inputs = open_inputs(signal_date)
    target = pd.Timestamp(signal_date).date().isoformat()
    if inputs.meta.get('asof') != target or inputs.meta.get('business_date') != target:
        raise InputBlocked('RQ_EXACT_DAY_IDENTITY_BINDING_MISSING')
    with duckdb.connect(str(inputs.db), read_only=True, config={'temp_directory': ''}) as con:
        tables = {r[0] for r in con.execute('SHOW TABLES').fetchall()}
        if 'identity_observed_today' not in tables:
            raise InputBlocked('RQ_EXACT_DAY_IDENTITY_MISSING')
        ids = con.execute('SELECT order_book_id FROM identity_observed_today').fetchall()
    if len(ids) != len({r[0] for r in ids}):
        raise InputBlocked('RQ_DUPLICATE_EXACT_DAY_IDENTITY')
    symbols = sorted({qmt_id(r[0]) for r in ids})
    if len(symbols) < 3000:
        raise InputBlocked('RQ_FULL_MARKET_TOO_SMALL')
    return symbols, []


def membership_frame(provider, observed_at, capture_id):
    """Three exact-date queries only. Caller owns the authenticated RQ session.

    Never persist credentials, synthesize past preopen evidence, or use provider
    creation time as our observation time. No order, price or download APIs here.
    """
    start = pd.Timestamp(observed_at)
    actual = pd.Timestamp(clock())
    if (start.tzinfo is None or start.date() != actual.date() or start > actual
            or start >= start.normalize() + pd.Timedelta(hours=9, minutes=30)):
        raise InputBlocked('RQ_PREOPEN_CAPTURE_TIME_INVALID')
    rows = []
    for code in ['000300.SH', '000905.SH', '000852.SH']:
        result = provider.index_components(rq_id(code), date=start.date(), return_create_tm=True)
        if not isinstance(result, (tuple, list)) or len(result) != 2:
            raise InputBlocked('RQ_COMPONENTS_RETURN_INVALID')
        members, created = result
        if not isinstance(members, list) or created is None:
            raise InputBlocked('RQ_COMPONENTS_SAMPLE_MISSING')
        created = pd.Timestamp(created)
        # RQ SDK index.py adds UTC+8 and returns a timezone-naive local timestamp.
        created = created.tz_localize(CST) if created.tzinfo is None else created.tz_convert(CST)
        observed = pd.Timestamp(clock()).isoformat()
        for member in members:
            rows.append({'capture_id': capture_id, 'snapshot_date': start.strftime('%Y%m%d'),
                         'effective_date': start.date().isoformat(), 'observed_at': observed,
                         'provider_created_at': created.isoformat(), 'index_code': code,
                         'index_name': code, 'symbol': qmt_id(member),
                         'source': 'RQDATA_COMPONENTS_PROVIDER', 'source_version': 'rqdatac3.7.1'})
    frame = pd.DataFrame(rows)
    validate_preopen_membership(frame, start.date())
    return frame
