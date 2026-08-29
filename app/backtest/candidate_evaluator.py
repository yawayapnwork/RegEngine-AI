"""Evaluates ONE historical transaction's `facts` against the NEW
candidate policy -- the core "run it through the OPA execution engine
using a newly generated policy bundle" requirement, implemented as two
interchangeable strategies behind one `CandidateEvaluator` protocol:

  1. `JsonLogicCandidateEvaluator` (default): in-process, via
     app.backtest.jsonlogic_evaluator. No OPA server, no network call, no
     risk whatsoever to any live system -- the candidate's
     `JsonLogicRule.logic` (app.compiler.jsonlogic_compiler output) is
     evaluated directly against the historical facts. This is the
     recommended path specifically BECAUSE it cannot touch production:
     the compiler already guarantees Rego and JSON-Logic evaluate
     identically given the same input (both modules' docstrings), so
     this is not a lesser approximation of "run it through OPA" -- it is
     evaluating the mathematically identical policy logic.
  2. `OpaCandidateEvaluator`: queries a real OPA server, for a deployment
     that wants literal Rego/OPA execution -- but MUST point at an
     isolated, backtest-only OPA instance (settings.backtest_opa_server_url),
     never `settings.opa_server_url` (the production sidecar). Publishing
     a not-yet-approved candidate bundle to the production OPA server,
     even under a distinct package name, is exactly the live-system risk
     this whole service exists to avoid taking.
"""
from __future__ import annotations

import logging
from typing import Protocol

from app.backtest.jsonlogic_evaluator import MissingFactError, UnsupportedJsonLogicNodeError, evaluate_jsonlogic
from app.execution.models import Decision
from app.execution.opa_engine import OPAEngine, OPAEngineError

logger = logging.getLogger(__name__)


class CandidateEvaluator(Protocol):
    async def evaluate(self, entity_type: str, facts: dict) -> tuple[str, list[str]]:
        """Returns (decision_value, violation_messages). decision_value is
        one of Decision's values ("allow"/"deny"/"flagged") -- "flagged"
        specifically means the candidate policy could not be evaluated
        (a fact it needs is absent from this historical transaction),
        mirroring OPA's own undefined-result semantics
        (app.execution.opa_engine's module docstring)."""
        ...


class JsonLogicCandidateEvaluator:
    def __init__(self, logic: dict, violation_message_template: str | None = None) -> None:
        self._logic = logic
        self._violation_message_template = violation_message_template

    async def evaluate(self, entity_type: str, facts: dict) -> tuple[str, list[str]]:
        data = {"entity_type": entity_type, "facts": facts}
        try:
            result = evaluate_jsonlogic(self._logic, data)
        except MissingFactError as exc:
            missing_path = exc.args[0]
            logger.debug("Candidate policy undefined for entity_type=%s: missing fact %s", entity_type, missing_path)
            return Decision.FLAGGED.value, [f"Candidate policy could not be evaluated: historical transaction has no '{missing_path}' fact."]
        except UnsupportedJsonLogicNodeError:
            logger.exception("Candidate JSON-Logic AST contains an unsupported node shape.")
            return Decision.FLAGGED.value, ["Candidate policy AST contains a node shape this backtest evaluator does not support."]

        if result:
            return Decision.ALLOW.value, []
        violation = (
            self._violation_message_template.format(**facts)
            if self._violation_message_template
            else "Candidate policy condition not satisfied."
        )
        return Decision.DENY.value, [violation]


class OpaCandidateEvaluator:
    def __init__(self, opa_engine: OPAEngine, package: str) -> None:
        self._opa = opa_engine
        self._package = package

    async def evaluate(self, entity_type: str, facts: dict) -> tuple[str, list[str]]:
        try:
            result = await self._opa.evaluate(self._package, {"entity_type": entity_type, "facts": facts})
        except OPAEngineError:
            logger.exception("Backtest OPA instance unreachable/errored for package=%s.", self._package)
            return Decision.FLAGGED.value, ["Backtest OPA instance was unreachable during replay."]

        if result is None:
            return Decision.FLAGGED.value, ["Candidate policy returned an undefined result for this historical transaction."]
        violations = list(result.get("violations", []) or [])
        return (Decision.DENY.value if violations else Decision.ALLOW.value), violations
