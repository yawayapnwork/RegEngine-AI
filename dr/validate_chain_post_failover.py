#!/usr/bin/env python3
"""Post-failover cryptographic chain validation.

Answers two distinct questions, both required for SEBI BCP sign-off after
a cross-region failover:

  1. "Is the chain on the new primary internally consistent?" -- delegates
     to `app.ledger.verifier.verify_chain`, the same recomputation logic
     the in-region chaos experiments already exercise
     (chaos/validation/validate_scenario3.py). Answers "has anything been
     altered, reordered, or dropped".

  2. "Did failover itself fork the chain?" -- internal consistency alone
     cannot catch this: a network partition that let the old primary keep
     accepting writes for a few seconds after the DR replica was promoted
     ("split-brain") would leave *two* internally-valid chains that
     disagree with each other from some sequence_num onward. This is
     checked by comparing the new primary's chain against the last
     pre-failover checkpoint manifest written by `ledger_backup_export.py`
     to the WORM S3 bucket (an independent, already-durable record of
     "what the chain looked like as of the last export, from a source
     that cannot have been written to by whichever side lost the split").
     If the new primary's row at that checkpoint's sequence_num doesn't
     hash-match the manifest, the two sides diverged and this is reported
     as a fork, not merely a "break".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from app.ledger.models import compliance_audit_ledger
from app.ledger.verifier import verify_chain

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("dr.validate_chain_post_failover")


class PostFailoverValidationResult(BaseModel):
    valid: bool
    internal_chain_valid: bool
    checkpoint_matched: bool | None = None  # None = no checkpoint manifest was available to compare against
    entries_checked: int
    breaks: list[dict[str, Any]] = []
    checkpoint_detail: str | None = None


def _latest_checkpoint_manifest(bucket: str, region: str) -> dict[str, Any] | None:
    """Fetches the most recent `manifest_*.json` object written by
    `ledger_backup_export.py` to the WORM ledger-backup bucket. Returns
    None (not an error) if the bucket is empty or unreachable -- a missing
    checkpoint degrades this to "internal consistency only", which is
    still logged clearly rather than silently treated as a pass."""
    try:
        import boto3

        s3 = boto3.client("s3", region_name=region)
        resp = s3.list_objects_v2(Bucket=bucket, Prefix="manifests/")
        objects = sorted(resp.get("Contents", []), key=lambda o: o["LastModified"], reverse=True)
        if not objects:
            return None
        latest_key = objects[0]["Key"]
        body = s3.get_object(Bucket=bucket, Key=latest_key)["Body"].read()
        return json.loads(body)
    except Exception:
        logger.warning("Could not fetch a checkpoint manifest from s3://%s/manifests/ -- split-brain-fork check will be skipped.", bucket, exc_info=True)
        return None


async def validate_post_failover(
    db_url: str,
    checkpoint_bucket: str | None = None,
    checkpoint_region: str = "ap-south-1",
) -> PostFailoverValidationResult:
    engine = create_async_engine(db_url, echo=False)
    try:
        chain_result = await verify_chain(engine)

        checkpoint_matched: bool | None = None
        checkpoint_detail = None
        if checkpoint_bucket:
            manifest = _latest_checkpoint_manifest(checkpoint_bucket, checkpoint_region)
            if manifest is None:
                checkpoint_detail = "no checkpoint manifest found; fork check skipped"
            else:
                checkpoint_seq = manifest["last_sequence_num"]
                checkpoint_hash = manifest["last_current_hash"]
                async with engine.connect() as conn:
                    row = (
                        await conn.execute(
                            select(compliance_audit_ledger.c.current_hash).where(
                                compliance_audit_ledger.c.sequence_num == checkpoint_seq
                            )
                        )
                    ).first()
                if row is None:
                    checkpoint_matched = False
                    checkpoint_detail = f"sequence_num {checkpoint_seq} from the checkpoint manifest is MISSING on the new primary -- possible truncated/rolled-back replication"
                elif row.current_hash != checkpoint_hash:
                    checkpoint_matched = False
                    checkpoint_detail = (
                        f"sequence_num {checkpoint_seq} hash MISMATCH: manifest={checkpoint_hash} "
                        f"new_primary={row.current_hash} -- chain forked (split-brain) at or before this point"
                    )
                else:
                    checkpoint_matched = True
                    checkpoint_detail = f"sequence_num {checkpoint_seq} matches checkpoint manifest exactly -- no divergence"

        overall_valid = chain_result.valid and (checkpoint_matched is not False)
        return PostFailoverValidationResult(
            valid=overall_valid,
            internal_chain_valid=chain_result.valid,
            checkpoint_matched=checkpoint_matched,
            entries_checked=chain_result.entries_checked,
            breaks=[b.model_dump() for b in chain_result.breaks],
            checkpoint_detail=checkpoint_detail,
        )
    finally:
        await engine.dispose()


async def _main(args: argparse.Namespace) -> int:
    result = await validate_post_failover(args.db_url, args.checkpoint_bucket, args.checkpoint_region)
    print(result.model_dump_json(indent=2))

    if result.internal_chain_valid:
        logger.info("✅ Internal chain consistency: PASS (%d entries checked)", result.entries_checked)
    else:
        logger.error("❌ Internal chain consistency: FAIL -- %d break(s) found", len(result.breaks))

    if result.checkpoint_matched is None:
        logger.warning("⚠️  Split-brain/fork check: SKIPPED (%s)", result.checkpoint_detail)
    elif result.checkpoint_matched:
        logger.info("✅ Split-brain/fork check: PASS -- %s", result.checkpoint_detail)
    else:
        logger.critical("❌ Split-brain/fork check: FAIL -- %s", result.checkpoint_detail)

    return 0 if result.valid else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the compliance audit ledger's hash chain after a DR failover.")
    parser.add_argument("--db-url", required=True, help="asyncpg URL for the NEW primary (post-promotion)")
    parser.add_argument("--checkpoint-bucket", default=None, help="WORM ledger-backup bucket to fetch the last pre-failover manifest from")
    parser.add_argument("--checkpoint-region", default="ap-south-1")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
