"""P0 recovery V0.1: one explicitly initialized, independent research database."""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
import hashlib
import json
import math

import duckdb
import pandas as pd
from research.forward_ret120 import initial_state, snapshot_sha256
from research.research_store import ResearchStore
from research.strategy_portfolio import DEEP_ID, RET120_ID, PORTFOLIO_ID, StrategyPortfolioPolicy, PilotReadinessPolicy

VERSION = "0.1-offline"
TIERS = {"CSI300": 300, "CSI500": 500, "CSI1000": 1000, "EX_CSI1800": None}


def fail(reason):
    raise ValueError("FAIL_CLOSED " + reason)


def date8(value):
    if type(value) is not str:
        fail("DATE_TYPE")
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            d = datetime.strptime(value, fmt)
            if d.strftime(fmt) == value:
                return d.strftime("%Y%m%d")
        except ValueError:
            pass
    fail("DATE_INVALID")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, allow_nan=False).encode()).hexdigest()


def calendar(profile, day):
    c = json.loads(Path(profile["calendar"]).read_text(encoding="utf-8-sig"))
    day = date8(day)
    if not c["valid_from"] <= day <= c["valid_to"]:
        fail("CALENDAR_UNKNOWN")
    d = datetime.strptime(day, "%Y%m%d")
    return not ((c.get("weekend_closed", True) and d.weekday() >= 5) or
                any(x["start"] <= day <= x["end"] for x in c["closed_ranges"]))


def previous_session(profile, day):
    d = datetime.strptime(date8(day), "%Y%m%d")
    for _ in range(370):
        d -= timedelta(days=1)
        if calendar(profile, d.strftime("%Y%m%d")):
            return d.strftime("%Y%m%d")
    fail("CALENDAR_UNKNOWN")


def profile_checked(profile):
    if profile.get("enabled") is not True:
        fail("RECOVERY_DISABLED")
    if profile.get("version") != VERSION:
        fail("PROFILE_VERSION")
    start = date8(profile["start_date"])
    if not calendar(profile, start):
        fail("START_NOT_TRADING_DAY")
    root = Path(profile["segment_root"]).resolve()
    db = Path(profile["segment_db"]).resolve()
    out = Path(profile["output_dir"]).resolve()
    if not db.is_relative_to(root) or db == root or not out.is_relative_to(root):
        fail("PATH_OUTSIDE_SEGMENT")
    if db == Path(profile["deep_db"]).resolve():
        fail("SHARED_DATABASE_FORBIDDEN")
    return start, db, out


def load_profile(path):
    p = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    profile_checked(p)
    return p


def positive_probe(result, business_date):
    """Accept the actual producer contract, never a stdout PASS substring."""
    if type(result.get("returncode")) is not int or result["returncode"] != 0:
        return False
    p = result.get("parsed_json")
    if not isinstance(p, dict) or p.get("status") != "RET120_UNIVERSE_PRIOR_FORWARD_V1_0_1_PROBE":
        return False
    try:
        if date8(p["signal_date"]) != date8(business_date):
            return False
        full = p["full_market_count"]
        if type(full) is not int or full < 3000 or p["csi1800_union"] != 1800:
            return False
        if type(p["csi1800_union"]) is not int or type(p["tail_count"]) is not int or p["tail_count"] != full - 1800:
            return False
        coverage = p["tier_coverage"]
        if not isinstance(coverage, dict) or set(coverage) != set(TIERS):
            return False
        for tier, expected in TIERS.items():
            v = coverage[tier]
            if not isinstance(v, dict) or any(type(v.get(k)) is not int for k in ("total", "valid_bucket", "need")):
                return False
            total, valid, need = v["total"], v["valid_bucket"], v["need"]
            if total != (expected if expected else full - 1800):
                return False
            if need != max(50, int(round(total * .80))) or not need <= valid <= total or v.get("pass") is not True:
                return False
        return p.get("mutation") is False and p.get("qmt_download_called") is False
    except (KeyError, TypeError, ValueError):
        return False


def read_connection(path):
    if not Path(path).is_file():
        fail("DATABASE_MISSING")
    return duckdb.connect(str(path), read_only=True, config={
        "autoinstall_known_extensions": "false", "autoload_known_extensions": "false",
        "temp_directory": "", "threads": "1",
    })


def segment_metadata(con, profile):
    row = con.execute("SELECT metadata FROM p0_recovery_segment").fetchall()
    if len(row) != 1:
        fail("SEGMENT_IDENTITY")
    m = json.loads(row[0][0])
    if m["profile_sha256"] != digest(profile):
        fail("PROFILE_MISMATCH")
    return m


def initialize_segment(profile, policy, artifacts, *, authorized=False, now=None):
    # Authorization is an explicit one-shot operation, never called by Daily Ops.
    start, db, out = profile_checked(profile)
    if authorized is not True:
        fail("INITIALIZATION_NOT_AUTHORIZED")
    today = (now or datetime.now()).strftime("%Y%m%d")
    if start <= today:
        fail("START_MUST_BE_FUTURE")
    if db.exists():
        fail("EXISTING_DATABASE_FORBIDDEN")
    root = Path(profile["segment_root"]).resolve()
    if root.exists() and any(root.iterdir()):
        fail("SEGMENT_DIRECTORY_NOT_EMPTY")
    cursor = previous_session(profile, start)
    state = initial_state(policy, artifacts)
    state.update(segment_start=start, segment_boundary_cursor=cursor,
                 last_processed_date=cursor)
    root.mkdir(parents=True, exist_ok=True)
    with ResearchStore(db) as store:
        store.con.execute("BEGIN TRANSACTION")
        try:
            store.con.execute("CREATE TABLE p0_recovery_segment(metadata VARCHAR NOT NULL)")
            store.con.execute("INSERT INTO p0_recovery_segment VALUES (?)", [json.dumps({
                "profile_sha256": digest(profile), "start_date": start,
                "boundary_cursor_only_not_a_session": cursor,
                "policy_hash": policy.fingerprint(artifacts),
                "old_holdings_inherited": False, "old_signals_inherited": False,
            })])
            store.save_state(policy.strategy_id, policy.fingerprint(artifacts), state)
            store.con.execute("COMMIT")
        except Exception:
            store.con.execute("ROLLBACK")
            raise
    return {"status": "PASS_P0_SEGMENT_INITIALIZED", "start_date": start,
            "boundary_cursor": cursor, "session_rows_created": 0}


def validate_pit(profile, day):
    from core import p0_rq_input as rq_input
    source = 'RQDATA_COMPONENTS_PROVIDER' if rq_input.enabled() else 'QMT_CURRENT'
    manifests = sorted(Path(profile["pit_root"]).glob(day + "T*_preopen.json"))
    if not manifests:
        fail("PIT_MISSING")
    # Do not silently fall back from the latest capture.
    m = json.loads(manifests[-1].read_text(encoding="utf-8-sig"))
    observed = datetime.fromisoformat(m["observed_at"])
    finished = datetime.fromisoformat(m["finished_at"])
    if (observed.utcoffset() != timedelta(hours=8) or finished.utcoffset() != timedelta(hours=8)
            or observed.strftime("%Y%m%d") != day or finished.strftime("%Y%m%d") != day
            or observed.strftime("%H%M%S") >= "092000" or finished.strftime("%H%M%S") >= "092000"
            or finished < observed or m.get("phase") != "preopen" or m.get("status") != "SUCCESS"):
        fail("PIT_TIME_OR_STATUS")
    selected = [x for x in m["items"] if x.get("dataset", "").startswith("index_membership_")]
    expected = {"000300.SH": 300, "000905.SH": 500, "000852.SH": 1000}
    if len(selected) != 3 or {x["dataset"].removeprefix("index_membership_") for x in selected} != set(expected):
        fail("PIT_MEMBERSHIP_MISSING")
    path = Path(selected[0]["file_path"]).resolve()
    if not path.is_relative_to(Path(profile["pit_root"]).resolve().parent):
        fail("PIT_PATH")
    for item in selected:
        idx = item["dataset"].removeprefix("index_membership_")
        if (Path(item["file_path"]).resolve() != path or sha(path) != item["sha256"]
                or item.get("source") != source
                or item["source_max_date"] != day or type(item["rows"]) is not int
                or item["rows"] != expected[idx] or item.get("health") != "FRESH"):
            fail("PIT_BINDING")
    frame = pd.read_parquet(path)
    if source == 'RQDATA_COMPONENTS_PROVIDER':
        # Same strict manifest/time/count checks above; additionally retain the
        # RQ provider-created/effective-date proof, never synthesize old PIT.
        rq_input.validate_preopen_membership(frame, day)
        # RQ rows record each provider response's actual observation time.
        # Bind those observations to this manifest's real capture interval;
        # never rewrite them to the capture start or relax the 09:20 cutoff.
        row_observed = pd.to_datetime(frame["observed_at"], utc=True)
        if ((row_observed < pd.Timestamp(observed)).any()
                or (row_observed > pd.Timestamp(finished)).any()):
            fail("PIT_OBSERVATION_OUTSIDE_CAPTURE")
    seen = set()
    for idx, n in expected.items():
        g = frame[frame["index_code"].eq(idx)]
        symbols = set(g["symbol"])
        if (len(g) != n or len(symbols) != n or seen & symbols
                or set(g["snapshot_date"].map(date8)) != {day}
                or (source != 'RQDATA_COMPONENTS_PROVIDER'
                    and set(g["observed_at"]) != {m["observed_at"]})
                or set(g["source"]) != {source}):
            fail("PIT_CONTENT")
        seen |= symbols
    return sha(manifests[-1])


def require_initialized(profile):
    _, db, _ = profile_checked(profile)
    with read_connection(db) as con:
        return segment_metadata(con, profile)


def segment_status(profile, *, now=None):
    _, db, _ = profile_checked(profile)
    with read_connection(db) as con:
        meta = segment_metadata(con, profile)
        row = con.execute("SELECT state_json,policy_hash FROM forward_state WHERE strategy_id=?", [RET120_ID]).fetchone()
        if not row or row[1] != meta["policy_hash"]:
            fail("RET120_POLICY_IDENTITY")
        state = json.loads(row[0])
        current = date8(state["last_processed_date"]) == (now or datetime.now()).strftime("%Y%m%d")
        if current:
            before_run(profile, state["last_processed_date"], now=now)
            sleeve(con, RET120_ID, state["last_processed_date"])
    return {"status": "OK", "health": state, "current_verified": current}


def ret120_status_current(result, day, *, recovery=False):
    p = result.get("parsed_json")
    if type(result.get("returncode")) is not int or result["returncode"] != 0 or not isinstance(p, dict) or p.get("status") != "OK":
        return False
    h = p.get("health")
    if not isinstance(h, dict) or h.get("status") != "COLLECTING":
        return False
    try:
        return (date8(h["last_processed_date"]) == date8(day) and
                all(type(h[k]) in (int, float) and math.isfinite(h[k]) and h[k] > 0
                    for k in ("base_equity", "shadow_equity")) and
                (not recovery or p.get("current_verified") is True))
    except (ValueError, TypeError, KeyError):
        return False


def before_run(profile, business_date, *, now=None):
    start, db, _ = profile_checked(profile)
    day = date8(business_date)
    clock = now or datetime.now()
    if day != clock.strftime("%Y%m%d") or day < start or not calendar(profile, day):
        fail("NOT_CURRENT_TRADING_SESSION")
    if clock.strftime("%H%M") < "1505":
        fail("CURRENT_SESSION_NOT_COMPLETED")
    with read_connection(db) as con:
        segment_metadata(con, profile)
    pit_sha = validate_pit(profile, day)
    return day, pit_sha


def valid_equity(row):
    e, cash, mv, gross = row[:4]
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in row[:4]):
        fail("INVALID_EQUITY")
    if e <= 0 or cash < 0 or mv < 0 or not 0 <= gross <= 1:
        fail("INVALID_EQUITY")
    if not math.isclose(e, cash + mv, abs_tol=1e-6) or not math.isclose(gross, mv / e, abs_tol=1e-9):
        fail("EQUITY_INCONSISTENT")


def sleeve(con, strategy, day, *, current=True):
    s = con.execute("SELECT universe_count,universe_source,benchmark_close,processed_at FROM forward_sessions WHERE strategy_id=? AND trade_date=?", [strategy, day]).fetchone()
    e = con.execute("SELECT equity,cash,gross_market_value,gross_weight FROM forward_equity WHERE strategy_id=? AND ledger='BASE20' AND trade_date=?", [strategy, day]).fetchone()
    if not s or type(s[0]) is not int or s[0] != 500 or not isinstance(s[1], str) or not s[1].strip() or not s[2] or not math.isfinite(s[2]) or s[2] <= 0 or not e:
        fail("SESSION_OR_SOURCE_MISSING")
    if not isinstance(s[3], datetime) or s[3].strftime("%Y%m%d") != day:
        fail("SESSION_OBSERVATION_DATE")
    valid_equity(e)
    if current:
        st = con.execute("SELECT state_json FROM forward_state WHERE strategy_id=?", [strategy]).fetchone()
        if not st or json.loads(st[0]).get("last_processed_date") != day:
            fail("STALE_STRATEGY_STATE")
    if strategy == RET120_ID:
        mf = con.execute("SELECT universe_count,valid_feature_count,q5_count,snapshot_sha256,universe_source,observed_at FROM ret120_forward_snapshot_manifest WHERE strategy_id=? AND signal_date=?", [strategy, day]).fetchone()
        snap = con.execute("SELECT * FROM ret120_forward_daily_snapshot WHERE strategy_id=? AND signal_date=?", [strategy, day]).fetchdf()
        if (not mf or mf[0] != 500 or len(snap) != 500 or mf[1] < 400 or
                int(snap["feature_valid"].sum()) != mf[1] or int(snap["selected_q5"].sum()) != mf[2] or
                mf[4] != s[1] or snapshot_sha256(snap) != mf[3] or not isinstance(mf[5], datetime) or mf[5].strftime("%Y%m%d") != day):
            fail("RET120_SNAPSHOT_SOURCE")
        proof = mf[3]
    else:
        proof = digest({"session": [*s[:3], s[3].isoformat()], "equity": e, "date": day})
    return tuple(e), proof


def inputs(profile, business_date, *, now=None):
    day, pit_sha = before_run(profile, business_date, now=now)
    with read_connection(profile["deep_db"]) as deep, read_connection(profile["segment_db"]) as ret:
        meta = segment_metadata(ret, profile)
        policy = ret.execute("SELECT policy_hash FROM forward_state WHERE strategy_id=?", [RET120_ID]).fetchone()
        if not policy or policy[0] != meta["policy_hash"]:
            fail("RET120_POLICY_IDENTITY")
        d, ds = sleeve(deep, DEEP_ID, day)
        r, rs = sleeve(ret, RET120_ID, day)
    return day, d, r, {"pit_sha256": pit_sha, "deep_source": ds, "ret120_snapshot": rs}


def check_portfolio(profile, business_date, *, now=None):
    day, _, _, _ = inputs(profile, business_date, now=now)
    return {"status": "PASS_P0_PORTFOLIO_CHECK", "ok": True, "business_date": day}


def portfolio_snapshot(profile, business_date, *, now=None):
    day, d, r, proof = inputs(profile, business_date, now=now)
    start, db, out = profile_checked(profile)
    # Separate storage preserves the original portfolio and its original ID unchanged.
    with duckdb.connect(str(db)) as con:
        segment_metadata(con, profile)
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute("CREATE TABLE IF NOT EXISTS p0_portfolio_baseline(start_date VARCHAR PRIMARY KEY, deep_equity DOUBLE, ret120_equity DOUBLE, profile_sha256 VARCHAR)")
            con.execute("CREATE TABLE IF NOT EXISTS p0_portfolio_equity(trade_date VARCHAR PRIMARY KEY, equity DOUBLE, cash DOUBLE, market_value DOUBLE, gross_weight DOUBLE, proof_json VARCHAR)")
            base = con.execute("SELECT * FROM p0_portfolio_baseline").fetchall()
            if not base:
                if day != start:
                    fail("BASELINE_SESSION_MISSING")
                con.execute("INSERT INTO p0_portfolio_baseline VALUES (?,?,?,?)", [start, d[0], r[0], digest(profile)])
                base = [(start, d[0], r[0], digest(profile))]
            if len(base) != 1 or base[0][0] != start or base[0][3] != digest(profile):
                fail("PORTFOLIO_BASELINE_CONFLICT")
            # Baseline source values are immutable and must still agree with source rows.
            with read_connection(profile["deep_db"]) as dc:
                bd, _ = sleeve(dc, DEEP_ID, start, current=False)
            br, _ = sleeve(con, RET120_ID, start, current=False)
            if (bd[0], br[0]) != (base[0][1], base[0][2]):
                fail("PORTFOLIO_BASELINE_CONFLICT")
            de, re = base[0][1:3]
            eq = 500000.0 * (d[0] / de + r[0] / re)
            cash = 500000.0 * (d[1] / de + r[1] / re)
            mv = 500000.0 * (d[2] / de + r[2] / re)
            gross = mv / eq
            valid_equity((eq, cash, mv, gross))
            record = (day, eq, cash, mv, gross, json.dumps(proof, sort_keys=True))
            old = con.execute("SELECT * FROM p0_portfolio_equity WHERE trade_date=?", [day]).fetchone()
            latest = con.execute("SELECT max(trade_date) FROM p0_portfolio_equity").fetchone()[0]
            if old:
                if tuple(old) != record or latest != day:
                    fail("PORTFOLIO_SAME_DAY_CONFLICT")
            else:
                if latest is not None and previous_session(profile, day) != latest:
                    fail("PORTFOLIO_DATE_GAP")
                con.execute("INSERT INTO p0_portfolio_equity VALUES (?,?,?,?,?,?)", record)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    result = {"status": "PASS_P0_PORTFOLIO_SNAPSHOT", "ok": True, "business_date": day,
              "last_aggregated_date": day, "segment_start": start, "portfolio_equity": eq,
              "portfolio_nav": eq / 1000000.0, "new_segment_not_old_portfolio_return": True}
    # Deterministic output: same-day retries repair publication without duplicate ledger rows.
    out.mkdir(parents=True, exist_ok=True)
    target = out / "latest.json"
    payload = json.dumps(result, sort_keys=True, allow_nan=False) + "\n"
    if not target.exists() or target.read_text(encoding="utf-8") != payload:
        temp = target.with_suffix(".tmp")
        temp.write_text(payload, encoding="utf-8")
        temp.replace(target)
    return result


def portfolio_status(profile, business_date, *, now=None):
    day, d, r, proof = inputs(profile, business_date, now=now)
    with read_connection(profile["segment_db"]) as con:
        row = con.execute("SELECT * FROM p0_portfolio_equity ORDER BY trade_date DESC LIMIT 1").fetchone()
        base = con.execute("SELECT deep_equity,ret120_equity FROM p0_portfolio_baseline WHERE start_date=?", [date8(profile["start_date"])]).fetchone()
    if not row or row[0] != day:
        fail("PORTFOLIO_NOT_CURRENT")
    valid_equity(row[1:5])
    expected = (500000.0 * (d[0] / base[0] + r[0] / base[1]),
                500000.0 * (d[1] / base[0] + r[1] / base[1]),
                500000.0 * (d[2] / base[0] + r[2] / base[1]))
    if tuple(row[1:4]) != expected or row[5] != json.dumps(proof, sort_keys=True):
        fail("PORTFOLIO_CURRENT_SOURCE_CONFLICT")
    public = json.loads((Path(profile["output_dir"]) / "latest.json").read_text(encoding="utf-8"))
    if public.get("last_aggregated_date") != day or public.get("portfolio_equity") != row[1]:
        fail("PORTFOLIO_OUTPUT_CONFLICT")
    return {**public, "status": "PASS_P0_PORTFOLIO_STATUS"}


def task_result_error(payload, expected_date, expected_status):
    if not isinstance(payload, dict) or payload.get("status") != expected_status or payload.get("ok") is not True:
        return "P0_BUSINESS_RESULT_INVALID"
    try:
        if date8(payload["business_date"]) != date8(expected_date):
            return "P0_BUSINESS_DATE_MISMATCH"
        if expected_status != "PASS_P0_PORTFOLIO_CHECK":
            if date8(payload["last_aggregated_date"]) != date8(expected_date):
                return "P0_STALE_PORTFOLIO"
            if type(payload["portfolio_equity"]) not in (int, float) or not math.isfinite(payload["portfolio_equity"]) or payload["portfolio_equity"] <= 0:
                return "P0_INVALID_EQUITY"
    except (KeyError, ValueError, TypeError):
        return "P0_BUSINESS_RESULT_INVALID"
    return None
