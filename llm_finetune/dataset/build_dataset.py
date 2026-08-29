#!/usr/bin/env python3
"""CLI: build the QLoRA instruction-tuning dataset from exported pipeline
artifacts.

Input is three JSONL files (one record per line):

  --audited-rules   AuditedComplianceRule dicts (app.agents.schemas) --
                     i.e. the Logic Auditor Agent's verdict on each
                     extraction. This is the source of truth for which
                     extractions are trustworthy enough to train on.
  --clause-chunks    ClauseChunk dicts (app.models), keyed by chunk_id,
                     supplying the verbatim clause text an extraction
                     example's prompt is built from.
  --compiled-rules   CompilationResult dicts (app.compiler.models),
                     supplying the Rego source for the rego_compile task.

Why JSONL exports rather than reading Postgres directly: the relational
schema (app.db.models.CompiledRule) deliberately persists only the
*compiled* output plus HITL status -- not the full ExtractedComplianceRule
JSON or the Logic Auditor's fidelity_score/findings, which exist only as
in-memory Pydantic objects during a compiler pipeline run (see
app.agents.pipeline). Point this script at whatever capture mechanism your
pipeline run used to dump those objects (a `--dump-artifacts` batch flag,
or fixtures saved from `evals/`) rather than assuming a DB column that
doesn't exist.

Usage:
  python llm_finetune/dataset/build_dataset.py \\
      --audited-rules artifacts/audited_rules.jsonl \\
      --clause-chunks artifacts/clause_chunks.jsonl \\
      --compiled-rules artifacts/compiled_rules.jsonl \\
      --out-dir llm_finetune/data
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from llm_finetune.dataset.format_instructions import build_dataset, split_train_val_test, write_jsonl

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("llm_finetune.build_dataset")


def _load_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Build QLoRA instruction-tuning JSONL from RegEngine AI pipeline artifacts.")
    parser.add_argument("--audited-rules", type=Path, required=True)
    parser.add_argument("--clause-chunks", type=Path, required=True)
    parser.add_argument("--compiled-rules", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--val-fraction", type=float, default=0.1)
    parser.add_argument("--test-fraction", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    audited_rules = _load_jsonl(args.audited_rules)
    clause_chunks = _load_jsonl(args.clause_chunks)
    compiled_rules = _load_jsonl(args.compiled_rules)
    logger.info("Loaded %d audited rules, %d clause chunks, %d compiled rules.", len(audited_rules), len(clause_chunks), len(compiled_rules))

    chunks_by_id = {c["chunk_id"]: c for c in clause_chunks}
    compiled_by_rule_id = {c["rule_id"]: c["rego"] for c in compiled_rules if c.get("compiled") and c.get("rego")}

    examples = build_dataset(audited_rules, chunks_by_id, compiled_by_rule_id)
    logger.info("Built %d instruction examples (extraction + rego_compile combined).", len(examples))

    task_counts: dict[str, int] = {}
    for e in examples:
        task_counts[e.task] = task_counts.get(e.task, 0) + 1
    logger.info("Task breakdown: %s", task_counts)

    train, val, test = split_train_val_test(examples, args.val_fraction, args.test_fraction, args.seed)
    logger.info("Split (by source rule, to avoid leakage): train=%d val=%d test=%d", len(train), len(val), len(test))

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_jsonl(train, str(args.out_dir / "train.jsonl"))
    write_jsonl(val, str(args.out_dir / "val.jsonl"))
    write_jsonl(test, str(args.out_dir / "test.jsonl"))
    logger.info("Wrote %s/{train,val,test}.jsonl", args.out_dir)


if __name__ == "__main__":
    main()
