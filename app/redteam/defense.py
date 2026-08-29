"""Requirement 2 -- Defense Middleware, the mechanical (non-LLM) half:
input sanitization and prompt boundary isolation, applied to a source
clause's text BEFORE it is ever embedded into an Extraction/Logic-
Auditor Agent prompt (see app.agents.crew.build_extraction_task, which
calls this module when `settings.redteam_defense_enabled` is True).

Two independent defenses, deliberately layered rather than combined
into one pass:

  1. `sanitize_source_text` -- detects and neutralizes known instruction-
     override / jailbreak phrasing and invisible-character tricks
     BEFORE the text is embedded in a prompt. This catches an attack
     whose payload text is designed to be read as an instruction no
     matter WHERE in the prompt it lands.

  2. `wrap_with_prompt_boundary` -- generates a per-call random nonce
     so the delimiter marking "everything between here is untrusted
     source text, not an instruction" can never be guessed or spoofed
     by the source document itself (Requirement 1's "delimiter escape"
     attack: a document containing a fake closing tag matching a
     STATIC delimiter, e.g. always `\"\"\"`, could trick a prompt into
     treating attacker text after the fake close as a new instruction
     block). This catches an attack that specifically targets the
     prompt's own structure, independent of what the injected text says.

Neither defense requires an LLM call, a network call, or any optional
dependency -- both are cheap enough to run on every ingested chunk
unconditionally once `redteam_defense_enabled` is on.
"""
from __future__ import annotations

import re
import secrets
import unicodedata

from pydantic import BaseModel

# Phrasing that shows up across real-world prompt-injection corpora
# (OWASP LLM01, published jailbreak collections) adapted to this
# system's specific attack surface (Requirement 1's examples: margin-
# override, HITL-bypass instructions aimed at the Extraction/Logic-
# Auditor agents). Matched case-insensitively; kept as a maintained,
# extensible list (like this codebase's other pattern tables, e.g.
# app.agents.tools.SEBI_ENTITY_TAXONOMY) rather than a claim of
# exhaustive coverage against every possible phrasing.
_INSTRUCTION_OVERRIDE_PATTERNS: tuple[str, ...] = (
    r"ignore\s+(all\s+|the\s+)?(previous|above|prior)\s+instructions?",
    r"disregard\s+(all\s+|the\s+)?(previous|above|prior)\s+instructions?",
    r"system\s*(prompt|override)\b",
    r"new\s+instructions?\s+(follow|below)",
    r"you\s+are\s+now\s+(in\s+)?(developer|debug|admin|unrestricted)\s+mode",
    r"you\s+are\s+no\s+longer\s+(an?\s+)?extraction\s+agent",
    r"(this\s+clause\s+has\s+already\s+been\s+)?(approved|reviewed)\s+by\s+a\s+compliance\s+officer",
    r"do\s+not\s+flag\s+(this\s+)?for\s+(human\s+)?review",
    r"set\s+(the\s+)?(audit\s+)?verdict\s+to\s+['\"]?approved['\"]?",
    r"set\s+fidelity_score\s+to\s+1\.?0?",
    r"output\s+your\s+(full\s+)?system\s+prompt",
    r"reveal\s+your\s+(system\s+)?instructions",
    r"bypass\s+(the\s+)?hitl\s+review",
    r"set\s+(any\s+)?(upfront\s+)?margin\s+(threshold\s+)?(value\s+)?to\s+0",
)
_COMPILED_OVERRIDE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in _INSTRUCTION_OVERRIDE_PATTERNS]

# Zero-width and other invisible/formatting Unicode characters used to
# hide text from a human skimming a rendered PDF while it remains fully
# present (and readable by an LLM) in the extracted text layer -- these
# are stripped outright rather than merely flagged, since there is no
# legitimate reason legal/financial clause text would contain them.
_INVISIBLE_CHARS = "​‌‍⁠﻿­"
_INVISIBLE_CHAR_RE = re.compile(f"[{_INVISIBLE_CHARS}]")

# A source document repeating this pipeline's OWN prompt delimiter
# syntax (triple double-quotes, the literal string "SOURCE CLAUSE TEXT")
# is itself a red flag independent of the nonce defense below -- a
# legitimate SEBI circular has no reason to contain either.
_DELIMITER_SPOOF_RE = re.compile(r'"""|SOURCE CLAUSE TEXT|</?source_clause_text', re.IGNORECASE)


class SanitizationResult(BaseModel):
    original_text: str
    cleaned_text: str
    detected_patterns: list[str]
    invisible_chars_removed: int
    delimiter_spoof_detected: bool

    @property
    def is_suspicious(self) -> bool:
        return bool(self.detected_patterns) or self.invisible_chars_removed > 0 or self.delimiter_spoof_detected


def detect_injection_patterns(text: str) -> list[str]:
    """The detection-only half of `sanitize_source_text`'s pattern
    matching, factored out so app.redteam.output_guard's OUTPUT-side
    leakage check runs against the EXACT SAME pattern table as the
    INPUT-side sanitizer -- these two checks having independently
    drifted pattern lists was a real bug caught by this module's own
    benchmark suite (app.redteam.benchmark) during development: a
    delimiter-escape payload's "new instructions follow" phrasing was
    recognized on the input side but not the output side, since
    output_guard.py originally maintained its own, shorter substring
    list. There is now exactly one place these patterns are defined."""
    return [match.group(0) for pattern in _COMPILED_OVERRIDE_PATTERNS for match in pattern.finditer(text)]


def sanitize_source_text(text: str) -> SanitizationResult:
    """Never raises and never REFUSES to return text -- a false positive
    here must not silently drop legitimate legal content (that would be
    its own compliance failure). Detected instruction-override phrases
    are neutralized in place (bracketed and labeled, not deleted
    outright) so a human reviewing `detected_patterns` can still see
    exactly what was caught and why, matching this codebase's
    established "flag, don't silently discard" philosophy
    (app.compiler.hitl's module docstring)."""
    normalized = unicodedata.normalize("NFKC", text)

    invisible_count = len(_INVISIBLE_CHAR_RE.findall(normalized))
    cleaned = _INVISIBLE_CHAR_RE.sub("", normalized)

    detected = detect_injection_patterns(cleaned)
    for pattern in _COMPILED_OVERRIDE_PATTERNS:
        cleaned = pattern.sub(lambda m: f"[REDACTED-POSSIBLE-INJECTION: {m.group(0)}]", cleaned)

    delimiter_spoof = bool(_DELIMITER_SPOOF_RE.search(cleaned))

    return SanitizationResult(
        original_text=text,
        cleaned_text=cleaned,
        detected_patterns=detected,
        invisible_chars_removed=invisible_count,
        delimiter_spoof_detected=delimiter_spoof,
    )


def wrap_with_prompt_boundary(text: str, tag: str = "source_clause_text") -> tuple[str, str]:
    """Returns `(wrapped_block, nonce)`. `wrapped_block` is ready to
    embed directly into a prompt template; `nonce` is returned
    separately purely for logging/telemetry (app.redteam.telemetry),
    never for re-parsing the model's own output -- this defense's job
    ends at making the boundary unguessable to the DOCUMENT, not at
    parsing the RESPONSE."""
    nonce = secrets.token_hex(8)
    opening = f"<{tag}_{nonce}>"
    closing = f"</{tag}_{nonce}>"
    return f"{opening}\n{text}\n{closing}", nonce
