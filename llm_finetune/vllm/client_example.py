#!/usr/bin/env python3
"""Minimal example client for the vLLM sidecar (docker-compose.vllm.yml),
showing the request shape needed to hit the fine-tuned "sebi-compliance"
LoRA adapter through vLLM's OpenAI-compatible `/v1/chat/completions`
endpoint, and validating the response back against the project's own
`ExtractedComplianceRule` schema so a bad generation fails loudly instead
of silently entering the pipeline.

This mirrors the same prompt shape `llm_finetune/dataset/format_instructions.py`
trained on (system prompt = EXTRACTION_SYSTEM_PROMPT, user content = clause
text) -- if this drifts from the training-time template, expect a
measurable quality regression even though nothing errors.

Usage:
  python llm_finetune/vllm/client_example.py --clause-text "..." --circular "SEBI/HO/.../2024/100" --clause-number "3.2"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import httpx

from app.agents.schemas import ExtractedComplianceRule
from llm_finetune.dataset.format_instructions import EXTRACTION_SYSTEM_PROMPT


def extract_via_local_llm(
    clause_text: str,
    circular_number: str | None,
    clause_number: str | None,
    base_url: str = "http://localhost:8000/v1",
    model: str = "sebi-compliance-llm",
) -> ExtractedComplianceRule:
    context_lines = []
    if circular_number:
        context_lines.append(f"Circular: {circular_number}")
    if clause_number:
        context_lines.append(f"Clause: {clause_number}")
    context = ("\n".join(context_lines) + "\n\n") if context_lines else ""

    resp = httpx.post(
        f"{base_url}/chat/completions",
        json={
            "model": model,
            "temperature": 0.1,  # matches llm_finetune/ollama/Modelfile's temperature choice; low for format-following tasks
            "max_tokens": 2048,
            "messages": [
                {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": f'{context}Clause text:\n"""\n{clause_text}\n"""'},
            ],
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    raw_content = resp.json()["choices"][0]["message"]["content"]

    # Validate immediately: a locally fine-tuned 7B/70B model producing
    # malformed JSON must be caught here, not three pipeline stages later
    # when app.compiler.rego_compiler chokes on a missing field.
    return ExtractedComplianceRule.model_validate_json(raw_content)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clause-text", required=True)
    parser.add_argument("--circular", default=None)
    parser.add_argument("--clause-number", default=None)
    parser.add_argument("--base-url", default="http://localhost:8000/v1")
    args = parser.parse_args()

    rule = extract_via_local_llm(args.clause_text, args.circular, args.clause_number, args.base_url)
    print(json.dumps(rule.model_dump(mode="json"), indent=2))


if __name__ == "__main__":
    main()
