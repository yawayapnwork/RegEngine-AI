"""Standalone SEBI-audit verification script.

    python -m app.ledger.verify_cli --start 2026-01-01 --end 2026-01-31
    python -m app.ledger.verify_cli                       # verify entire chain

Prints a JSON `ChainVerificationResult` to stdout and exits 0 if the chain
is intact over the requested range, or 1 if any break was found — suitable
for wiring into a scheduled compliance job that alerts on a non-zero exit.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys

from app.ledger.db import get_ledger_engine
from app.ledger.verifier import verify_chain


def _parse_date(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


async def _main(start: dt.datetime | None, end: dt.datetime | None) -> int:
    engine = get_ledger_engine()
    result = await verify_chain(engine, start_time=start, end_time=end)
    print(result.model_dump_json(indent=2))
    return 0 if result.valid else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the RegEngine AI compliance audit ledger's hash chain.")
    parser.add_argument("--start", type=_parse_date, default=None, help="ISO 8601 range start (inclusive), e.g. 2026-01-01")
    parser.add_argument("--end", type=_parse_date, default=None, help="ISO 8601 range end (inclusive), e.g. 2026-01-31")
    args = parser.parse_args()

    exit_code = asyncio.run(_main(args.start, args.end))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
