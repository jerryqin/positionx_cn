from __future__ import annotations
from collections import defaultdict
import math
import random
import statistics
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


def _first(record: Mapping[str, Any], names: Sequence[str], required: bool = True):
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    if required:
        raise KeyError(f"none of fields {list(names)} found; keys={sorted(record.keys())}")
    return None


def normalize_date(v: Any) -> str:
    s = str(v).replace("-", "").replace("/", "")[:8]
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"invalid date: {v!r}")
    return s


def normalize_candidate(record: Mapping[str, Any], source: str) -> Dict[str, Any]:
    symbol = str(_first(record, ["symbol"]))
    entry = normalize_date(_first(record, ["entry_date", "entry_session"]))
    exit_ = normalize_date(_first(record, ["exit_date", "exit_session"]))
    net = float(_first(record, [
        "counterfactual_net_return",
        "net_return",
        "return_net",
        "net_return_20d",
    ]))
    signal = _first(record, ["signal_date"], required=False)
    return {
        "symbol": symbol,
        "signal_date": normalize_date(signal) if signal is not None else None,
        "entry_date": entry,
        "exit_date": exit_,
        "net_return": net,
        "source": source,
    }


def extract_candidates(sim_result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    trades = list(sim_result.get("trades") or [])
    skipped = list(sim_result.get("skipped") or [])
    out = [normalize_candidate(x, "executed") for x in trades]
    out.extend(normalize_candidate(x, "skipped") for x in skipped)
    out.sort(key=lambda x: (x["entry_date"], x["symbol"], x["exit_date"]))
    return out


def replay_selection(
    candidates: Sequence[Mapping[str, Any]],
    max_positions: int,
    mode: str = "SYMBOL_ASC",
    seed: int = 0,
) -> Dict[str, Any]:
    if max_positions <= 0:
        raise ValueError("max_positions must be positive")

    by_day: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        by_day[str(c["entry_date"])].append(dict(c))

    rng = random.Random(seed)
    active: List[Dict[str, Any]] = []
    selected: List[Dict[str, Any]] = []
    skipped_no_slot: List[Dict[str, Any]] = []
    skipped_duplicate: List[Dict[str, Any]] = []

    for day in sorted(by_day):
        # Exit-at-close positions remain occupied at this day's open.
        active = [x for x in active if str(x["exit_date"]) >= day]
        held = {str(x["symbol"]) for x in active}

        todays = list(by_day[day])
        if mode == "SYMBOL_ASC":
            todays.sort(key=lambda x: str(x["symbol"]))
        elif mode == "RANDOM":
            todays.sort(key=lambda x: str(x["symbol"]))  # deterministic pre-order
            rng.shuffle(todays)
        else:
            raise ValueError(f"unknown mode: {mode}")

        for c in todays:
            sym = str(c["symbol"])
            if sym in held:
                skipped_duplicate.append(c)
                continue
            if len(active) >= max_positions:
                skipped_no_slot.append(c)
                continue
            selected.append(c)
            active.append(c)
            held.add(sym)

    return {
        "max_positions": max_positions,
        "mode": mode,
        "seed": seed if mode == "RANDOM" else None,
        "selected": selected,
        "skipped_no_slot": skipped_no_slot,
        "skipped_duplicate": skipped_duplicate,
    }


def return_stats(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    vals = [float(x["net_return"]) for x in rows if x.get("net_return") is not None and math.isfinite(float(x["net_return"]))]
    if not vals:
        return {"n": 0, "avg": None, "median": None, "win_rate": None, "profit_factor": None}
    gains = sum(v for v in vals if v > 0)
    losses = -sum(v for v in vals if v < 0)
    pf = gains / losses if losses > 0 else None
    return {
        "n": len(vals),
        "avg": statistics.fmean(vals),
        "median": statistics.median(vals),
        "win_rate": sum(1 for v in vals if v > 0) / len(vals),
        "profit_factor": pf,
    }


def summarize_replay(replay: Mapping[str, Any], total_candidates: int) -> Dict[str, Any]:
    sel = list(replay["selected"])
    ns = list(replay["skipped_no_slot"])
    dup = list(replay["skipped_duplicate"])
    return {
        "max_positions": int(replay["max_positions"]),
        "mode": replay["mode"],
        "seed": replay.get("seed"),
        "coverage": len(sel) / total_candidates if total_candidates else 0.0,
        "selected": return_stats(sel),
        "skipped_no_slot": return_stats(ns),
        "skipped_duplicate": return_stats(dup),
        "counts": {
            "selected": len(sel),
            "no_slot": len(ns),
            "duplicate": len(dup),
            "total": total_candidates,
        },
    }


def percentile(values: Sequence[float], q: float):
    xs = sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    w = pos - lo
    return xs[lo] * (1 - w) + xs[hi] * w


def distribution(values: Sequence[float]) -> Dict[str, Any]:
    xs = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "min": min(xs),
        "p05": percentile(xs, 0.05),
        "p25": percentile(xs, 0.25),
        "median": percentile(xs, 0.50),
        "p75": percentile(xs, 0.75),
        "p95": percentile(xs, 0.95),
        "max": max(xs),
        "mean": statistics.fmean(xs),
    }


def random_audit(
    candidates: Sequence[Mapping[str, Any]],
    capacity: int,
    seeds: int,
) -> Dict[str, Any]:
    avgs, pfs, wins, coverages = [], [], [], []
    for seed in range(seeds):
        s = summarize_replay(
            replay_selection(candidates, capacity, mode="RANDOM", seed=seed),
            len(candidates),
        )
        avgs.append(s["selected"]["avg"])
        pfs.append(s["selected"]["profit_factor"])
        wins.append(s["selected"]["win_rate"])
        coverages.append(s["coverage"])
    return {
        "capacity": capacity,
        "seeds": seeds,
        "avg_net_return": distribution(avgs),
        "profit_factor": distribution(pfs),
        "win_rate": distribution(wins),
        "coverage": distribution(coverages),
    }
