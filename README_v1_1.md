# Deep Oversold CSI500 Forward v1.1 + Research Store v1.2

This patch is intended to be applied **before the first true Forward session**.

## Storage v1.2

Large immutable foundation data is no longer physically copied into DuckDB.
It remains as durable local CSV/Parquet and is exposed through DuckDB external views.
Mutable Forward journal tables remain physical DuckDB tables.

If bootstrap v1 was interrupted, rebuild the empty research store safely:

```powershell
python scripts\bootstrap_research_store.py --rebuild
```

`--rebuild` refuses to delete a store that already contains Forward journal rows.

## Frozen protocol v1.1

Authoritative human-readable protocol:

`config/research/deep_oversold_csi500_forward_v1_1.yaml`

Ambiguities are frozen as follows:

- Exit: signal=T close; S1=first market session after signal and entry at S1 open;
  exit at S20 close. This is the exact semantics used by the accepted CSI300/500/1000
  historical tests. Changing to S21 would be a new protocol and requires rerunning history.
- Cost: 20bp and 50bp are **round-trip totals**, not per side.
- Fillability: `STANDARD_OPEN_V1` matches the historical `can_buy_at_open` semantics;
  no-limit days are not automatically rejected.
- Lot sizing: 1% is a target/ceiling. Shares are rounded down to the QMT minimum buy lot.
  If zero lots fit, the signal is recorded but entry is `BLOCKED_MIN_LOT_OR_CAPACITY`.
  The engine never forces one lot above the 1% target.

## Run

```powershell
python apply_deep_oversold_forward_v1_1_storage_v1_2.py
python -m unittest discover -s tests -p "test_deep_oversold_csi500_forward.py" -v
python -m unittest discover -s tests -p "test_research_store_v12.py" -v
python -m unittest discover -s tests -v
python scripts\bootstrap_research_store.py --rebuild
python scripts\run_deep_oversold_csi500_forward.py
```
