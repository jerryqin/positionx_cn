"""Pure transformations of captured evidence; no SDK or PositionX imports."""

import math


# Candidate old-environment field contract, NOT verified against new Full-QMT.
CONTRACT = {
    "account": {"account_id": "id", "cash": "number", "frozen_cash": "number",
                "market_value": "number", "total_asset": "number"},
    "positions": {"account_id": "id", "stock_code": "symbol", "volume": "quantity",
                  "can_use_volume": "quantity", "open_price": "number",
                  "avg_price": "number", "market_value": "number"},
    "orders": {"account_id": "id", "order_id": "id", "stock_code": "symbol",
               "order_type": "integer", "order_volume": "quantity", "price": "number",
               "traded_volume": "quantity", "order_status": "integer",
               "order_time": "integer"},
    "trades": {"account_id": "id", "traded_id": "id", "order_id": "id",
               "stock_code": "symbol", "order_type": "integer", "traded_price": "number",
               "traded_volume": "quantity", "traded_amount": "number",
               "traded_time": "integer"},
}


def valid(field, rule):
    value, raw_type = field.get("value"), field.get("raw_type")
    if rule == "id":
        return (raw_type in {"builtins.str", "builtins.int"}
                and field.get("source_valid") is True
                and type(value) is str and value.startswith("redacted:") and len(value) == 73)
    if rule == "symbol":
        return raw_type == "builtins.str" and type(value) is str and not value.startswith("redacted:")
    if rule in {"integer", "quantity"}:
        return raw_type == "builtins.int" and type(value) is int and (rule != "quantity" or value >= 0)
    return (raw_type in {"builtins.int", "builtins.float"}
            and type(value) in (int, float) and math.isfinite(value))


def normalize(domain, raw):
    """Separate successful return from the availability/validity of field samples."""
    contract = CONTRACT[domain]
    result = {"domain": domain, "raw_query": "NOT_COVERED",
              "schema_coverage": "NOT_COVERED", "items": [], "reasons": []}
    if raw.get("kind") == "none":
        result["reasons"].append("returned_none_semantics_unknown")
        return result
    expected = "object" if domain == "account" else "collection"
    if raw.get("kind") != expected:
        result.update(raw_query="FAIL", schema_coverage="FAIL")
        result["reasons"].append("unexpected_query_shape")
        return result
    result["raw_query"] = "PASS"
    items = raw["items"]
    if not items:
        result["reasons"].append("no_field_samples")
        return result
    failed = False
    for item in items:
        fields = item["public_fields"]
        missing = sorted(set(contract) - set(fields))
        invalid = sorted(name for name, rule in contract.items()
                         if name in fields and not valid(fields[name], rule))
        failed |= bool(missing or invalid or item["capture_errors"])
        result["items"].append({
            "normalized": {name: fields[name]["value"] for name in contract if name in fields},
            "field_map": {name: name for name in contract if name in fields},
            "missing_fields": missing, "invalid_fields": invalid,
            "capture_errors": item["capture_errors"],
            "warnings": ["SDK enums/time/quantity units are not live-verified"],
        })
    result["schema_coverage"] = "FAIL" if failed else "PASS"
    return result
