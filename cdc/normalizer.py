"""Legacy Data Transformer & Normalizer for CDC Stream Ingestion.

Converts un-normalized Debezium SQL change records from legacy Broking ERPs
(MS SQL Server / Oracle) into standard `TransactionPayload` objects consumable by
RegEngine AI's FastAPI evaluation engine.

Legacy Data Mappings Handled:
  - Column Slugging & Standardizing: `N_TRANS_ID` -> `transaction_id`, `VC_BROKER_CODE` -> `broker_id`
  - Timestamp Normalization: `YYYYMMDDHHMMSS` string or epoch millis -> ISO 8601 UTC
  - Enum Decoding: `N_CLIENT_CAT: 1 -> RETAIL, 2 -> HNI, 3 -> INSTITUTIONAL`
  - Basis Points Scale: `N_UPFRONT_MARGIN_BP: 2000 -> 20.0%`
  - Paisa Money Scale: `N_ORDER_PRICE_Paisa: 250000 -> 2500.00 INR`
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.execution.models import SourceChannel, TransactionPayload

logger = logging.getLogger("cdc_normalizer")


class LegacyNormalizerError(ValueError):
    """Raised when a legacy DB payload is unparseable or fails validation rules."""


CLIENT_CATEGORY_MAP = {
    1: "RETAIL",
    2: "HNI",
    3: "INSTITUTIONAL",
}


def parse_legacy_timestamp(val: Any) -> dt.datetime:
    """Parses legacy timestamp formats: YYYYMMDDHHMMSS, YYYY-MM-DD HH:MM:SS, ISO strings, or epoch millis."""
    if isinstance(val, (int, float)):
        # Epoch millis or seconds
        if val > 1_000_000_000_000:
            val = val / 1000.0
        return dt.datetime.fromtimestamp(val, dt.timezone.utc)

    if not isinstance(val, str) or not val.strip():
        raise LegacyNormalizerError("Timestamp field is missing or empty.")

    s = val.strip()

    # Match YYYYMMDDHHMMSS (14 digits)
    if re.match(r"^\d{14}$", s):
        try:
            return dt.datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
        except ValueError as exc:
            raise LegacyNormalizerError(f"Invalid YYYYMMDDHHMMSS timestamp '{s}': {exc}") from exc

    # Match standard ISO / SQL datetime
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        pass

    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise LegacyNormalizerError(f"Unrecognized timestamp format '{s}': {exc}") from exc


class LegacyNormalizer:
    """Normalizes raw Debezium CDC records into RegEngine AI TransactionPayload instances."""

    @classmethod
    def normalize_event(cls, raw_event: dict[str, Any]) -> TransactionPayload | None:
        if not isinstance(raw_event, dict):
            raise LegacyNormalizerError("Raw CDC event must be a JSON object dictionary.")

        op = raw_event.get("op", raw_event.get("operation"))
        if op == "d":
            logger.info("Ignoring CDC delete event (no post-image row available).")
            return None

        # Debezium envelope carries row image in 'after' (or payload root if pre-extracted)
        row = raw_event.get("after") or raw_event.get("payload") or raw_event
        source_info = raw_event.get("source", {})
        source_table = source_info.get("table", raw_event.get("source_table", "TBL_TRADE_TRANSACTIONS"))

        if not isinstance(row, dict) or not row:
            raise LegacyNormalizerError(f"CDC event for table '{source_table}' contains no valid 'after' row image.")

        table_upper = source_table.upper()
        if "TRADE_TRANSACTIONS" in table_upper or "TBL_TRADE" in table_upper:
            return cls._normalize_trade_transaction(row, source_table)
        elif "CLIENT_COLLATERAL" in table_upper or "COLLATERAL" in table_upper:
            return cls._normalize_client_collateral(row, source_table)
        else:
            # Generic fallback for standard transaction table shapes
            return cls._normalize_generic_row(row, source_table)

    @classmethod
    def _normalize_trade_transaction(cls, row: dict[str, Any], table: str) -> TransactionPayload:
        trans_id_raw = row.get("N_TRANS_ID") or row.get("transaction_id") or row.get("id")
        if trans_id_raw is None:
            raise LegacyNormalizerError(f"Table '{table}' missing mandatory primary key 'N_TRANS_ID'.")

        broker_code = str(row.get("VC_BROKER_CODE") or row.get("broker_id") or "UNKNOWN_BROKER")
        client_cat_code = row.get("N_CLIENT_CAT", 1)
        client_category = CLIENT_CATEGORY_MAP.get(client_cat_code, "RETAIL")

        margin_bp = row.get("N_UPFRONT_MARGIN_BP", 2000)
        upfront_margin_pct = float(margin_bp) / 100.0  # e.g., 2000 BP -> 20.0%

        peak_flag = row.get("N_PEAK_MARGIN_FLAG", 1)
        peak_margin_collected = bool(int(peak_flag) == 1)

        price_paisa = float(row.get("N_ORDER_PRICE_Paisa", 0))
        qty = int(row.get("N_ORDER_QTY", 1))
        order_value_inr = (price_paisa * qty) / 100.0  # Paisa to INR

        trade_time_raw = row.get("DT_TRADE_TIME") or row.get("created_at") or "20260828230000"
        received_at = parse_legacy_timestamp(trade_time_raw)

        facts = {
            "upfront_margin_pct": upfront_margin_pct,
            "peak_margin_collected": peak_margin_collected,
            "client_category": client_category,
            "order_value_inr": order_value_inr,
            "collateral_haircut_applied": True,
            "symbol": str(row.get("VC_SYMBOL", "NIFTY")),
            "segment_type": str(row.get("VC_SEGMENT_TYPE", "CASH")),
        }

        return TransactionPayload(
            transaction_id=f"CDC-TRADE-{trans_id_raw}",
            entity_type="Stockbroker",
            facts=facts,
            source_channel=SourceChannel.DB_CDC,
            broker_id=broker_code,
            received_at=received_at,
            metadata={"source_table": table, "legacy_client_code": str(row.get("VC_CLIENT_CODE", ""))},
        )

    @classmethod
    def _normalize_client_collateral(cls, row: dict[str, Any], table: str) -> TransactionPayload:
        alloc_id_raw = row.get("N_ALLOC_ID") or row.get("id")
        if alloc_id_raw is None:
            raise LegacyNormalizerError(f"Table '{table}' missing mandatory primary key 'N_ALLOC_ID'.")

        broker_code = str(row.get("VC_BROKER_CODE") or "UNKNOWN_BROKER")
        cash_paisa = float(row.get("N_CASH_COLLATERAL", 0))
        noncash_paisa = float(row.get("N_NONCASH_COLLATVAL", 0))
        total_collateral_inr = (cash_paisa + noncash_paisa) / 100.0

        haircut_bp = row.get("N_HAIRCUT_PCT_BP", 2000)
        haircut_pct = float(haircut_bp) / 100.0

        alloc_time_raw = row.get("DT_ALLOCATION_TIME") or "20260828230000"
        received_at = parse_legacy_timestamp(alloc_time_raw)

        facts = {
            "upfront_margin_pct": max(15.0, 100.0 - haircut_pct),
            "peak_margin_collected": True,
            "client_category": "RETAIL",
            "order_value_inr": total_collateral_inr,
            "collateral_haircut_applied": bool(haircut_pct > 0),
            "haircut_pct": haircut_pct,
        }

        return TransactionPayload(
            transaction_id=f"CDC-COLLATERAL-{alloc_id_raw}",
            entity_type="Stockbroker",
            facts=facts,
            source_channel=SourceChannel.DB_CDC,
            broker_id=broker_code,
            received_at=received_at,
            metadata={"source_table": table, "legacy_client_code": str(row.get("VC_CLIENT_CODE", ""))},
        )

    @classmethod
    def _normalize_generic_row(cls, row: dict[str, Any], table: str) -> TransactionPayload:
        trans_id = str(row.get("transaction_id", row.get("id", f"GENERIC-{hash(str(row))}")))
        broker_id = str(row.get("broker_id", row.get("VC_BROKER_CODE", "DEFAULT_BROKER")))
        facts = {k: v for k, v in row.items() if k not in ("transaction_id", "id", "broker_id", "entity_type")}

        if "upfront_margin_pct" not in facts:
            facts["upfront_margin_pct"] = 20.0
        if "peak_margin_collected" not in facts:
            facts["peak_margin_collected"] = True

        return TransactionPayload(
            transaction_id=f"CDC-ROW-{trans_id}",
            entity_type=str(row.get("entity_type", "Stockbroker")),
            facts=facts,
            source_channel=SourceChannel.DB_CDC,
            broker_id=broker_id,
            metadata={"source_table": table},
        )
