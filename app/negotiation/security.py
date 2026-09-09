"""Security mechanisms for the Multi-Agent Arbitration subsystem.

Guarantees:
1. UNTRUSTED DATA BOUNDARY: Regulatory PDF text is treated as untrusted external user data.
2. PROMPT INJECTION PREVENTION: Detects adversarial attempts embedded within regulatory text
   to override system instructions, hijack agent personas, or force false compliance approvals.
3. DELIMITER ISOLATION: Enforces XML/boundary token encapsulation with anti-breakout escaping.
"""
from __future__ import annotations

import html
import re

# Boundary delimiter tags for strict prompt sandboxing
UNTRUSTED_START_TAG = '<untrusted_regulatory_source_text untrusted="true">'
UNTRUSTED_END_TAG = "</untrusted_regulatory_source_text>"

# Patterns indicative of prompt injection attacks inside regulatory documents
_INJECTION_PATTERNS = [
    re.compile(r"(?i)\b(?:ignore|disregard|forget|override)\s+(?:all\s+)?(?:previous|prior|system|above)\s+(?:instructions|prompts|rules|commands)"),
    re.compile(r"(?i)\b(?:you\s+are\s+now|act\s+as)\s+(?:a|an)?\s*(?:unrestricted|unfiltered|jailbroken|helpful\s+assistant|developer|admin)"),
    re.compile(r"(?i)\b(?:system\s+prompt|new\s+system\s+instructions|system\s+override)\b"),
    re.compile(r"(?i)\b(?:bypass|skip|disable)\s+(?:hitl|human[-_ ]in[-_ ]the[-_ ]loop|compliance\s+checks?|approval\s+gate)\b"),
    re.compile(r"(?i)\b(?:always\s+return\s+allow|set\s+verdict\s*(?:to|=)\s*approved|force\s+approval|mark\s+compliant\s+unconditionally)\b"),
    re.compile(r"(?i)<\s*/?\s*(?:system|prompt|instruction|untrusted_regulatory_source_text)\s*>"),
    re.compile(r"<\|im_start\|>|<\|im_end\|>|\[INST\]|\[/INST\]"),
]


def inspect_for_prompt_injection(text: str | None) -> tuple[bool, list[str]]:
    """Scans untrusted regulatory text for adversarial prompt injection payloads.

    Returns:
        (is_injection_detected, list_of_matching_descriptions)
    """
    if not text:
        return False, []

    findings: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            matched_snippet = match.group(0)
            findings.append(f"Suspicious adversarial pattern detected: '{matched_snippet}'")

    return bool(findings), findings


def isolate_untrusted_source_text(raw_text: str | None, clause_id: str | None = None) -> str:
    """Safely encapsulates raw regulatory text within explicit boundary tags
    and neutralizes delimiter breakout attempts.
    """
    if not raw_text:
        return f'{UNTRUSTED_START_TAG}\n(empty clause text)\n{UNTRUSTED_END_TAG}'

    # Neutralize closing delimiter breakout attempts
    escaped_text = raw_text.replace(UNTRUSTED_END_TAG, html.escape(UNTRUSTED_END_TAG))
    escaped_text = escaped_text.replace("<|im_end|>", "[ESCAPED_IM_END]")
    escaped_text = escaped_text.replace("[/INST]", "[ESCAPED_INST]")

    cid_attr = f' clause_id="{clause_id}"' if clause_id else ""
    return (
        f'<untrusted_regulatory_source_text untrusted="true"{cid_attr}>\n'
        f"{escaped_text.strip()}\n"
        f"{UNTRUSTED_END_TAG}"
    )
