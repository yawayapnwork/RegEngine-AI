"""Converts RegEngine AI's own pipeline artifacts -- parsed SEBI clause
chunks (`app.models.ClauseChunk`), extraction-agent AST outputs
(`app.agents.schemas.ExtractedComplianceRule` / `AuditedComplianceRule`),
and compiled OPA modules (`app.compiler.models.CompiledRego`) -- into
chat-format instruction-tuning JSONL.

Why these artifacts and not a hand-written dataset: the production system
(CrewAI + Claude, see `app.agents.crew`) already produces exactly the
input/output pairs a domain-adapted local model needs to imitate --
clause text in, structured JSON out; structured JSON in, Rego out. Every
example here is therefore something the *real* pipeline actually did and
a human (via the Logic Auditor Agent / HITL review) already had a chance
to catch if it was wrong -- see `_only_trustworthy` below, which is the
single most important filter in this module.

Two task families, matching the two structured-output stages of the
compiler pipeline:

  - "extraction": clause text -> `ExtractedComplianceRule` JSON.
    Teaches SEBI terminology, obligation-type classification
    (shall/shall not/may), and the verbatim-evidence discipline.
  - "rego_compile": `ExtractedComplianceRule` JSON -> Rego module.
    Teaches the exact Rego package/rule-naming conventions
    `app.compiler.rego_compiler` uses, so a fine-tuned model's output is
    something the rest of the pipeline (OPA, `app.execution.opa_engine`)
    can consume unmodified.

Both are emitted as OpenAI/Llama-3/Mistral-style chat records
(`{"messages": [...]}`), the format `trl.SFTTrainer` and vLLM's chat
template both consume natively.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Literal

TaskType = Literal["extraction", "rego_compile"]

EXTRACTION_SYSTEM_PROMPT = (
    "You are a SEBI securities-law compliance analyst. Given a verbatim clause "
    "from a SEBI circular, extract its regulatory obligations as a single JSON "
    "object matching the ExtractedComplianceRule schema: target_entities, "
    "trigger_conditions, deterministic_logic (numeric thresholds only), "
    "qualitative_directives, obligation_type (mandatory/prohibited/conditional/"
    "recommended), and ambiguous_spans. Every extracted field MUST include a "
    "verbatim_evidence quote copied exactly from the clause -- never infer or "
    "round a number that is not explicitly stated. Output ONLY the JSON object, "
    "no commentary."
)

REGO_SYSTEM_PROMPT = (
    "You are a compiler that translates structured SEBI compliance rules "
    "(ExtractedComplianceRule JSON, containing deterministic_logic thresholds) "
    "into Open Policy Agent Rego modules. Follow the house convention exactly: "
    "`package sebi.circulars.<circular>.clause_<clause>`, `import rego.v1`, "
    "`default allow := false`, one `cond_N` rule per threshold, an `allow` rule "
    "requiring `entity_matches` and every `cond_N`, a `violation contains msg` "
    "rule for the negated condition, and a `decision` object exposing allow, "
    "violations, and rule_id. Output ONLY the Rego source, no commentary."
)


@dataclass
class ChatExample:
    task: TaskType
    source_rule_id: str
    messages: list[dict[str, str]]

    def to_jsonl_record(self) -> dict[str, Any]:
        return {"task": self.task, "source_rule_id": self.source_rule_id, "messages": self.messages}


def _only_trustworthy(audited_rules: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Filters to `AuditedComplianceRule` dicts the Logic Auditor Agent
    actually approved. Training on a REJECTED or NEEDS_REVISION extraction
    would teach the model to reproduce the exact hallucinations/errors the
    audit stage exists to catch -- the whole point of this dataset is
    imitating the *validated* pipeline output, not raw first-pass drafts.
    """
    for record in audited_rules:
        verdict = record.get("audit", {}).get("verdict")
        fidelity = record.get("audit", {}).get("fidelity_score", 0.0)
        if verdict == "approved" and fidelity >= 0.85:
            yield record
        # "needs_revision"/"rejected" records are deliberately dropped, not
        # down-weighted -- see module docstring.


def _clause_text_from_chunk(chunk: dict[str, Any]) -> str:
    """Reconstructs the verbatim clause text an extraction was performed
    against, from a persisted `ClauseChunk`-shaped dict (as produced by
    `app.parsing.chunker`). Falls back to `extraction_notes`/evidence
    reassembly only if the raw chunk text was not retained -- prefer
    always persisting `raw_text` alongside extractions specifically so
    this fallback path stays unused in practice."""
    if "raw_text" in chunk and chunk["raw_text"]:
        return chunk["raw_text"]
    raise ValueError(f"ClauseChunk {chunk.get('chunk_id', '?')} has no raw_text; cannot build an extraction example without the source clause.")


def build_extraction_example(chunk: dict[str, Any], extracted_rule: dict[str, Any]) -> ChatExample:
    clause_text = _clause_text_from_chunk(chunk)
    context_lines = []
    if extracted_rule.get("circular_number"):
        context_lines.append(f"Circular: {extracted_rule['circular_number']}")
    if extracted_rule.get("clause_number"):
        context_lines.append(f"Clause: {extracted_rule['clause_number']}")
    context = ("\n".join(context_lines) + "\n\n") if context_lines else ""

    # Strip fields that are pipeline bookkeeping, not something the model
    # should be trained to hallucinate a value for (it never sees the DB).
    target_output = {k: v for k, v in extracted_rule.items() if k not in ("source_chunk_id", "source_sha256")}

    return ChatExample(
        task="extraction",
        source_rule_id=extracted_rule["rule_id"],
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": f"{context}Clause text:\n\"\"\"\n{clause_text}\n\"\"\""},
            {"role": "assistant", "content": json.dumps(target_output, sort_keys=True, ensure_ascii=False)},
        ],
    )


def build_rego_example(extracted_rule: dict[str, Any], compiled_rego: dict[str, Any]) -> ChatExample:
    input_json = {k: v for k, v in extracted_rule.items() if k not in ("source_chunk_id", "source_sha256")}
    return ChatExample(
        task="rego_compile",
        source_rule_id=extracted_rule["rule_id"],
        messages=[
            {"role": "system", "content": REGO_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(input_json, sort_keys=True, ensure_ascii=False)},
            {"role": "assistant", "content": compiled_rego["rego_code"]},
        ],
    )


def build_dataset(
    audited_rules: list[dict[str, Any]],
    chunks_by_id: dict[str, dict[str, Any]],
    compiled_by_rule_id: dict[str, dict[str, Any]],
) -> list[ChatExample]:
    """Assembles the full example set from three joined artifact
    collections (as they'd be exported from Postgres -- see
    `build_dataset.py`'s `--export-from-db` path).

    `chunks_by_id`: ClauseChunk.chunk_id -> chunk dict.
    `compiled_by_rule_id`: rule_id -> CompiledRego dict (only rules that
    successfully compiled produce a "rego_compile" example; a rule that
    was fully HITL-flagged with no compiled output teaches nothing useful
    to this task).
    """
    examples: list[ChatExample] = []
    for record in _only_trustworthy(audited_rules):
        rule = record["rule"]
        chunk = chunks_by_id.get(rule["source_chunk_id"])
        if chunk is None:
            continue  # source clause text unavailable -- skip rather than fabricate context
        try:
            examples.append(build_extraction_example(chunk, rule))
        except ValueError:
            continue

        compiled = compiled_by_rule_id.get(rule["rule_id"])
        if compiled is not None and compiled.get("rego_code"):
            examples.append(build_rego_example(rule, compiled))

    return examples


def split_train_val_test(
    examples: list[ChatExample],
    val_fraction: float = 0.1,
    test_fraction: float = 0.1,
    seed: int = 42,
) -> tuple[list[ChatExample], list[ChatExample], list[ChatExample]]:
    """Splits by `source_rule_id` (not by example) so the two examples
    derived from the same rule (extraction + rego_compile) never end up on
    opposite sides of the split -- that would let the rego_compile
    training example leak the exact extraction the model is later
    evaluated on producing."""
    rule_ids = sorted({e.source_rule_id for e in examples})
    rng = random.Random(seed)
    rng.shuffle(rule_ids)

    n = len(rule_ids)
    n_val = int(n * val_fraction)
    n_test = int(n * test_fraction)
    val_ids = set(rule_ids[:n_val])
    test_ids = set(rule_ids[n_val : n_val + n_test])

    train = [e for e in examples if e.source_rule_id not in val_ids and e.source_rule_id not in test_ids]
    val = [e for e in examples if e.source_rule_id in val_ids]
    test = [e for e in examples if e.source_rule_id in test_ids]
    return train, val, test


def write_jsonl(examples: list[ChatExample], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for example in examples:
            fh.write(json.dumps(example.to_jsonl_record(), ensure_ascii=False) + "\n")
