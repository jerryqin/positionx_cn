# Edge Discovery #2 v0.2.2 — Adjusted Price Representation Fix

The v0.2.1 run exposed the actual remaining issue: many QMT `front` adjusted
series contain negative historical adjusted price levels. Negative front-adjusted
levels can be mathematically valid artifacts of additive front adjustment, but
price-ratio features such as `close/close.shift(60)-1` are not valid on such
levels.

Therefore v0.2.2 does not whitelist negative prices. Instead, the discovery
scanner requests QMT `dividend_type="back_ratio"`.

Why:
- ratio-style adjustment preserves positive price levels;
- corporate-action-adjusted price ratios remain meaningful;
- all Edge features and forward labels in this scanner are ratio/return based;
- the economic hypothesis is unchanged.

The integrity gate remains fail-closed:
- paired 0/0 suspension/no-price rows -> NaN/NaN;
- one-sided zero / negative / infinite / nonnumeric -> reject;
- catastrophic >10x or <0.1x adjusted gaps -> quarantine;
- post-gate abs(excess)>100x -> block;
- no winsorization / return clipping.

Compatibility fix:
`quarantine_histories()` again returns exactly two values, so the existing
v0.2 test suite is not broken. The runner uses
`quarantine_histories_with_audit()` for the third sanitation report.

Run:

```powershell
python apply_edge2_trend_multiuniverse_v022_fix.py
python -m unittest discover -s tests -p "test_trend_edge_scan_v022.py" -v
python -m unittest discover -s tests -v
python scripts\run_edge_discovery_trend_replication_v022.py
```

Do not use `--refresh-missing-history` unless the runner explicitly reports
`BLOCKED_INCOMPLETE_HISTORY`.
