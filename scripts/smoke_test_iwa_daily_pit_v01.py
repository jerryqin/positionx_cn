from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    cfg_root = Path(
        os.environ.get(
            "IWAOS_DATA_ROOT",
            str(Path.home() / ".iwaos"),
        )
    )
    cfg = cfg_root / "storage" / "db-connections.json"

    print("python:", sys.executable)
    try:
        import iwa_data
    except Exception as e:
        print("IWA_DATA_IMPORT=FAIL", e)
        return 2

    print("iwa_data:", getattr(iwa_data, "__file__", "?"))
    print("config exists:", cfg.exists(), cfg)

    if not cfg.exists():
        return 2

    try:
        conns = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception as e:
        print("CONFIG_READ=FAIL", e)
        return 2

    conn = next(
        (x for x in conns if x.get("type") == "iwaos-cloud"),
        None,
    )
    if not conn:
        print("CLOUD_CONFIG=FAIL")
        return 2

    print(
        "plan=", conn.get("yuhuPlanCode"),
        "status=", conn.get("yuhuSubscriptionStatus"),
        "expires=", conn.get("yuhuPlanExpiredAt"),
    )
    print("token present:", bool(conn.get("yuhuToken")))

    try:
        df = iwa_data.cloud_query(
            "SELECT stock_code,trade_date,up_limit,down_limit "
            "FROM iwaos_stock_limit LIMIT 1"
        )
    except Exception as e:
        print("QUERY=FAIL", e)
        return 1

    print("rows:", len(df))
    print("LOCAL_DAILY_PIT_AUTOMATION=READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
