#!/usr/bin/env python3
"""Scheduled export of the hash-chained audit ledger into WORM (Object
Lock) S3, cross-region replicated by terraform/modules/ledger_backup.

Two objects per run:
  - `entries/<start_seq>-<end_seq>.jsonl`: the exported rows verbatim
    (including their stored hashes -- this is a durable copy, not a
    re-derivation).
  - `manifests/manifest_<timestamp>_<end_seq>.json`: `{last_sequence_num,
    last_current_hash, exported_at}` -- the checkpoint
    `dr/validate_chain_post_failover.py` compares the new primary against
    after a failover to detect a chain fork.

Idempotent and incremental: tracks the last exported sequence_num in a
small `ledger_backup_state` table so a re-run (or the next scheduled run)
only exports what's new. Meant to run frequently (e.g. every 5-15 minutes
via cron/Celery beat) -- the export cadence is effectively your ledger's
worst-case fork-detection granularity after a split-brain failover.

Usage:
  python dr/ledger_backup_export.py --db-url postgresql+asyncpg://... \\
      --bucket regengine-prod-ledger-backup-primary --region ap-south-1
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import Column, MetaData, String, Table, select
from sqlalchemy.dialects.postgresql import BIGINT
from sqlalchemy.ext.asyncio import create_async_engine

from app.ledger.models import compliance_audit_ledger

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dr.ledger_backup_export")

_state_metadata = MetaData()
ledger_backup_state = Table(
    "ledger_backup_state",
    _state_metadata,
    Column("exporter_id", String(64), primary_key=True),
    Column("last_exported_sequence_num", BIGINT, nullable=False),
)


def _row_to_json(row: dict) -> dict:
    out = dict(row)
    for key, value in out.items():
        if isinstance(value, dt.datetime):
            out[key] = value.isoformat()
    return out


async def _get_last_exported_sequence(conn, exporter_id: str) -> int:
    row = (
        await conn.execute(
            select(ledger_backup_state.c.last_exported_sequence_num).where(
                ledger_backup_state.c.exporter_id == exporter_id
            )
        )
    ).first()
    return row.last_exported_sequence_num if row else -1


async def export_new_entries(db_url: str, bucket: str, region: str, exporter_id: str = "primary-export") -> dict | None:
    import boto3

    engine = create_async_engine(db_url, echo=False)
    s3 = boto3.client("s3", region_name=region)

    try:
        async with engine.begin() as conn:
            await conn.run_sync(_state_metadata.create_all, checkfirst=True)
            last_seq = await _get_last_exported_sequence(conn, exporter_id)

            query = (
                select(compliance_audit_ledger)
                .where(compliance_audit_ledger.c.sequence_num > last_seq)
                .order_by(compliance_audit_ledger.c.sequence_num.asc())
            )
            rows = (await conn.execute(query)).mappings().all()

            if not rows:
                logger.info("No new ledger entries since sequence_num=%d. Nothing to export.", last_seq)
                return None

            start_seq = rows[0]["sequence_num"]
            end_seq = rows[-1]["sequence_num"]
            last_hash = rows[-1]["current_hash"]

            ndjson_lines = [json.dumps(_row_to_json(dict(r)), sort_keys=True) for r in rows]
            body = "\n".join(ndjson_lines).encode("utf-8")
            body_sha256 = hashlib.sha256(body).hexdigest()

            entries_key = f"entries/{start_seq:012d}-{end_seq:012d}.jsonl"
            s3.put_object(
                Bucket=bucket,
                Key=entries_key,
                Body=body,
                ContentType="application/x-ndjson",
                Metadata={"sha256": body_sha256, "row-count": str(len(rows))},
            )

            manifest = {
                "exporter_id": exporter_id,
                "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "start_sequence_num": start_seq,
                "last_sequence_num": end_seq,
                "last_current_hash": last_hash,
                "entries_object_key": entries_key,
                "entries_sha256": body_sha256,
                "row_count": len(rows),
            }
            timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            manifest_key = f"manifests/manifest_{timestamp}_{end_seq:012d}.json"
            s3.put_object(
                Bucket=bucket,
                Key=manifest_key,
                Body=json.dumps(manifest, indent=2).encode("utf-8"),
                ContentType="application/json",
            )

            if last_seq >= 0:
                await conn.execute(
                    ledger_backup_state.update()
                    .where(ledger_backup_state.c.exporter_id == exporter_id)
                    .values(last_exported_sequence_num=end_seq)
                )
            else:
                await conn.execute(
                    ledger_backup_state.insert().values(exporter_id=exporter_id, last_exported_sequence_num=end_seq)
                )

            logger.info(
                "Exported sequence_num [%d, %d] (%d rows) to s3://%s/%s ; manifest s3://%s/%s",
                start_seq, end_seq, len(rows), bucket, entries_key, bucket, manifest_key,
            )
            return manifest
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Export the RegEngine AI audit ledger to WORM S3 for cross-region DR.")
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--region", default="ap-south-1")
    parser.add_argument("--exporter-id", default="primary-export")
    args = parser.parse_args()

    manifest = asyncio.run(export_new_entries(args.db_url, args.bucket, args.region, args.exporter_id))
    if manifest is None:
        sys.exit(0)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
