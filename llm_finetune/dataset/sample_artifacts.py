#!/usr/bin/env python3
"""Generates a small synthetic set of audited_rules/clause_chunks/compiled_rules
JSONL fixtures, shaped exactly like real pipeline artifacts, so
`build_dataset.py` and `train_qlora.py` can be smoke-tested end-to-end
without a live CrewAI extraction run or a populated Postgres instance.

Mirrors the fixture-generator convention already used for the compliance
evaluation harness (see evals/synthetic_trade_generator.py) -- this is the
LLM-fine-tuning-pipeline equivalent, not a replacement for training on the
real, audited pipeline output before shipping an actual model.

Usage:
  python llm_finetune/dataset/sample_artifacts.py --count 40 --out-dir artifacts/
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

_CLAUSE_TEMPLATES = [
    (
        "Every stock broker shall collect upfront margin of not less than {pct}% "
        "of the transaction value from the client, in the form of cash, cash "
        "equivalent or approved securities, before carrying out the transaction.",
        "Upfront Margin",
        ">=",
        "%",
    ),
    (
        "Client funds and securities shall be segregated from the stock broker's own "
        "funds and securities at all times, and shall not be used for any purpose "
        "other than meeting the respective client's obligations.",
        None,
        None,
        None,
    ),
    (
        "The stock broker shall report the daily margin trading facility details to "
        "the stock exchange within {hours} hours of the end of trading.",
        "Reporting Window",
        "<=",
        "hours",
    ),
]


def _make_example(i: int) -> tuple[dict, dict, dict]:
    circular = f"SEBI/HO/MIRSD/DOP/CIR/P/2024/{100 + i}"
    clause_number = f"{(i % 9) + 1}.{(i % 4) + 1}"
    rule_id = f"{circular}:{clause_number}:{i}"
    chunk_id = f"chunk-{i:05d}"

    template, metric, operator, unit = random.choice(_CLAUSE_TEMPLATES)
    if metric == "Upfront Margin":
        value = random.choice([20, 25, 30])
        text = template.format(pct=value)
        thresholds = [
            {
                "metric": metric,
                "operator": operator,
                "value": float(value),
                "value_upper": None,
                "unit": unit,
                "applies_to": "Stockbroker",
                "verbatim_evidence": f"upfront margin of not less than {value}%",
            }
        ]
    elif metric == "Reporting Window":
        value = random.choice([24, 48])
        text = template.format(hours=value)
        thresholds = [
            {
                "metric": metric,
                "operator": operator,
                "value": float(value),
                "value_upper": None,
                "unit": unit,
                "applies_to": "Stockbroker",
                "verbatim_evidence": f"within {value} hours",
            }
        ]
    else:
        text = template
        thresholds = []

    extracted_rule = {
        "rule_id": rule_id,
        "source_chunk_id": chunk_id,
        "source_sha256": f"{i:064x}",
        "circular_number": circular,
        "clause_number": clause_number,
        "section_path": [clause_number.split(".")[0], clause_number],
        "target_entities": [
            {"raw_text": "stock broker", "normalized_entity": "Stockbroker", "verbatim_evidence": "stock broker"}
        ],
        "trigger_conditions": [],
        "deterministic_logic": thresholds,
        "qualitative_directives": [] if thresholds else [
            {"directive_text": "Client funds must be segregated from broker's own funds.", "verbatim_evidence": "shall be segregated from the stock broker's own funds"}
        ],
        "obligation_type": "mandatory",
        "extraction_confidence": round(random.uniform(0.9, 0.99), 2),
        "ambiguous_spans": [],
        "extraction_notes": None,
    }

    audited_rule = {
        "rule": extracted_rule,
        "audit": {
            "rule_id": rule_id,
            "verdict": "approved",
            "fidelity_score": round(random.uniform(0.9, 1.0), 2),
            "findings": [],
            "verified_quote_count": max(1, len(thresholds)),
            "unverified_quote_count": 0,
            "audited_at": "2026-01-15T00:00:00+00:00",
        },
        "revision_round": 0,
    }

    clause_chunk = {
        "chunk_id": chunk_id,
        "circular_number": circular,
        "clause_number": clause_number,
        "raw_text": text,
    }

    package = f"sebi.circulars.{circular.replace('/', '_').lower()}.clause_{clause_number.replace('.', '_')}"
    if thresholds:
        cond_lines = "\n".join(
            f'cond_{j} if {{ input.facts.{t["metric"].lower().replace(" ", "_")} {t["operator"]} {t["value"]} }}'
            for j, t in enumerate(thresholds)
        )
        allow_body = "\n    ".join(["entity_matches"] + [f"cond_{j}" for j in range(len(thresholds))])
        rego_code = f"""package {package}

import rego.v1

default allow := false

entity_matches if {{ input.entity_type == "Stockbroker" }}

{cond_lines}

allow if {{
    {allow_body}
}}

violation contains msg if {{
    entity_matches
    not allow
    msg := "Clause {clause_number} of {circular} not satisfied"
}}

deny := violation

decision := {{"allow": allow, "violations": violation, "rule_id": "{rule_id}"}}
"""
        compiled = True
    else:
        rego_code = None
        compiled = False

    compilation_result = {
        "rule_id": rule_id,
        "compiled": compiled,
        "rego": {"rule_id": rule_id, "package": package, "rego_code": rego_code, "thresholds_compiled": len(thresholds)} if compiled else None,
        "json_logic": None,
        "hitl_flags": [],
    }

    return audited_rule, clause_chunk, compilation_result


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic pipeline-artifact fixtures for LLM fine-tuning smoke tests.")
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--out-dir", type=Path, default=Path("artifacts"))
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    random.seed(args.seed)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    audited, chunks, compiled = [], [], []
    for i in range(args.count):
        a, c, r = _make_example(i)
        audited.append(a)
        chunks.append(c)
        compiled.append(r)

    (args.out_dir / "audited_rules.jsonl").write_text("\n".join(json.dumps(x) for x in audited) + "\n", encoding="utf-8")
    (args.out_dir / "clause_chunks.jsonl").write_text("\n".join(json.dumps(x) for x in chunks) + "\n", encoding="utf-8")
    (args.out_dir / "compiled_rules.jsonl").write_text("\n".join(json.dumps(x) for x in compiled) + "\n", encoding="utf-8")
    print(f"Wrote {args.count} synthetic examples to {args.out_dir}/")


if __name__ == "__main__":
    main()
