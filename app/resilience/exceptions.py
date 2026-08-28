"""Typed failure categories for the Dead-Letter Queue.

Two axes, deliberately kept separate:

  WHERE it failed      -> FailureCategory (app.resilience.models) --
                           which stage of the pipeline, used to route a
                           DLQ entry to the right queue/inspector view and
                           to pick the right task to re-dispatch on requeue.
  WHETHER to retry it   -> app.resilience.retry_policy.is_transient(exc) --
                           a network blip is worth three attempts with
                           backoff; a PDF that is genuinely unparseable, or
                           a JSON-Logic AST that is genuinely malformed,
                           will fail exactly the same way on attempt four
                           as it did on attempt one. Retrying it anyway
                           just delays the operator finding out AND wastes
                           an LLM call / a worker slot per wasted attempt.

The exceptions below exist for the SECOND axis specifically: they mark a
failure as "not exception TYPE tells you category, exception INSTANCE
tells you don't bother retrying" at the few call sites where that
distinction isn't already obvious from an existing exception type (e.g.
`app.parsing.exceptions.UnsupportedFileError` already unambiguously means
"never retry"; these new ones cover the gaps).
"""
from __future__ import annotations


class NonRetryableError(Exception):
    """Base class for a failure a retry can never fix. Routed to the DLQ
    immediately, with zero retry attempts consumed -- retrying it would
    only add latency before an operator sees the same failure a human
    needs to look at anyway."""


class LLMExtractionError(NonRetryableError):
    """The Extraction/Logic-Auditor Agent pipeline produced no usable
    result for a clause that isn't a transient LLM-API issue (rate limit,
    timeout, connection drop -- those raise ordinary network exceptions
    and ARE retried, see app.resilience.retry_policy.is_transient). This
    covers e.g. the agent's own output failing schema validation after
    every internal revision attempt (app.agents.crew's MAX_REVISION_ROUNDS
    exhausted) -- more inference calls will not fix a source clause the
    model cannot structure."""


class MalformedASTError(NonRetryableError):
    """A compiled JSON-Logic AST (app.compiler.models.JsonLogicRule.logic)
    failed structural validation -- see
    app.compiler.jsonlogic_validator.validate_json_logic_ast. Indicates a
    bug in the compiler itself (a threshold/operator combination that
    produced an invalid node shape), not a data quality issue a retry or
    a different LLM call could fix."""
