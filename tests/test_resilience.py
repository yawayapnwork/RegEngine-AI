"""Tests for app.resilience: the retry-transience classifier, the AST
validator, and the DLQ store's full lifecycle (send -> list/filter ->
edit -> requeue/discard/resolve, plus its invalid-transition guards).

Redis is faked throughout (mirrors the _FakeRedis pattern used across this
repo's test suite); no live Celery worker, Redis, or Postgres needed.
"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.compiler.jsonlogic_validator import validate_json_logic_ast, validate_json_serializable
from app.resilience.dead_letter_queue import DeadLetterQueue, DLQEntryNotFoundError, DLQInvalidTransitionError
from app.resilience.exceptions import MalformedASTError
from app.resilience.models import DLQStatus, FailureCategory
from app.resilience.retry_policy import compute_backoff_delay, is_transient


# --------------------------------------------------------------------------
# is_transient
# --------------------------------------------------------------------------


class TestIsTransient:
    def test_httpx_transport_error_is_transient(self):
        assert is_transient(httpx.ConnectError("refused")) is True

    def test_connection_error_is_transient(self):
        assert is_transient(ConnectionError("reset")) is True

    def test_timeout_error_is_transient(self):
        assert is_transient(TimeoutError("timed out")) is True

    def test_value_error_is_not_transient(self):
        assert is_transient(ValueError("bad data")) is False

    def test_custom_exception_is_not_transient(self):
        class SomeAppError(Exception):
            pass

        assert is_transient(SomeAppError("permanent")) is False

    def test_transient_cause_chain_is_detected(self):
        """This codebase frequently wraps a lower-level failure in a typed
        exception ('raise TypedError(...) from original_exc') -- the
        classifier must look through __cause__, not just the outer type."""
        try:
            try:
                raise ConnectionError("upstream refused")
            except ConnectionError as inner:
                raise RuntimeError("wrapped") from inner
        except RuntimeError as outer:
            assert is_transient(outer) is True

    def test_no_cause_chain_cycle_hangs_forever(self):
        # Defensive: an exception can't actually have itself as __cause__
        # in normal Python, but the classifier's cycle guard (`seen` set)
        # must not infinite-loop if it ever did.
        exc = ValueError("self-referential")
        exc.__cause__ = exc
        assert is_transient(exc) is False


class TestComputeBackoffDelay:
    def test_delay_never_exceeds_max(self):
        for attempt in range(20):
            delay = compute_backoff_delay(attempt, base_seconds=2.0, max_delay_seconds=60.0)
            assert 0 <= delay <= 60.0

    def test_negative_attempt_raises(self):
        with pytest.raises(ValueError):
            compute_backoff_delay(-1)

    def test_later_attempts_have_a_higher_ceiling_until_capped(self):
        # Not a strict inequality on any single draw (it's random), but the
        # theoretical ceiling (base * 2**attempt) must grow monotonically
        # until max_delay_seconds caps it.
        import app.resilience.retry_policy as rp

        assert min(rp.compute_backoff_delay(0, base_seconds=1, max_delay_seconds=1000) for _ in range(50)) < \
            max(rp.compute_backoff_delay(5, base_seconds=1, max_delay_seconds=1000) for _ in range(50))


# --------------------------------------------------------------------------
# JSON-Logic AST validator
# --------------------------------------------------------------------------


class TestValidateJsonLogicAst:
    def test_valid_nested_ast_passes(self):
        ast = {"and": [{">=": [{"var": "facts.upfront_margin_pct"}, 20]}, {"==": [{"var": "entity_type"}, "Stockbroker"]}]}
        validate_json_logic_ast(ast)  # must not raise

    def test_literal_passes(self):
        validate_json_logic_ast(42)
        validate_json_logic_ast("hello")
        validate_json_logic_ast(True)
        validate_json_logic_ast(None)

    def test_list_of_valid_nodes_passes(self):
        validate_json_logic_ast([{"var": "x"}, 5, "text"])

    def test_multi_key_operator_dict_is_malformed(self):
        with pytest.raises(MalformedASTError, match="exactly one key"):
            validate_json_logic_ast({"and": [], "or": []})

    def test_empty_dict_is_malformed(self):
        with pytest.raises(MalformedASTError):
            validate_json_logic_ast({})

    def test_non_string_operator_key_is_malformed(self):
        # Can't construct a dict with a non-string key from JSON, but a
        # Python caller (a compiler bug) could pass one directly.
        with pytest.raises(MalformedASTError):
            validate_json_logic_ast({1: ["x"]})

    def test_error_path_identifies_the_nested_failure(self):
        with pytest.raises(MalformedASTError, match=r"\$\.and\[1\]"):
            validate_json_logic_ast({"and": [{"==": [1, 1]}, {"bad": 1, "extra": 2}]})

    def test_unsupported_type_is_malformed(self):
        with pytest.raises(MalformedASTError):
            validate_json_logic_ast(object())


class TestValidateJsonSerializable:
    def test_normal_ast_round_trips(self):
        validate_json_serializable({"==": [{"var": "x"}, 1]})  # must not raise

    def test_nan_is_rejected(self):
        with pytest.raises(MalformedASTError):
            validate_json_serializable({"==": [{"var": "x"}, float("nan")]})

    def test_non_serializable_object_is_rejected(self):
        with pytest.raises(MalformedASTError):
            validate_json_serializable({"==": [{"var": "x"}, object()]})


# --------------------------------------------------------------------------
# DeadLetterQueue
# --------------------------------------------------------------------------


class _FakePipeline:
    def __init__(self, redis: "_FakeDLQRedis") -> None:
        self._redis = redis
        self._ops: list[tuple] = []

    def set(self, key, value):
        self._ops.append(("set", key, value))
        return self

    def zadd(self, key, mapping):
        self._ops.append(("zadd", key, mapping))
        return self

    def zrem(self, key, member):
        self._ops.append(("zrem", key, member))
        return self

    async def execute(self):
        for op in self._ops:
            if op[0] == "set":
                _, key, value = op
                self._redis.strings[key] = value
            elif op[0] == "zadd":
                _, key, mapping = op
                self._redis.zsets.setdefault(key, {}).update(mapping)
            elif op[0] == "zrem":
                _, key, member = op
                self._redis.zsets.get(key, {}).pop(member, None)
        self._ops.clear()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDLQRedis:
    """Minimal async stand-in for the redis.asyncio.Redis subset
    DeadLetterQueue uses."""

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}

    def pipeline(self):
        return _FakePipeline(self)

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(self, key: str, value: str) -> None:
        self.strings[key] = value

    async def zrevrange(self, key: str, start: int, end: int) -> list[str]:
        members = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1], reverse=True)
        ids = [m for m, _ in members]
        if end == -1:
            return ids[start:]
        return ids[start : end + 1]

    async def zcard(self, key: str) -> int:
        return len(self.zsets.get(key, {}))


@pytest.fixture
def dlq() -> DeadLetterQueue:
    return DeadLetterQueue(_FakeDLQRedis(), key_prefix="test:dlq")


@pytest.mark.asyncio
class TestDeadLetterQueueSendAndGet:
    async def test_send_then_get_round_trips(self, dlq):
        entry = await dlq.send(
            category=FailureCategory.PDF_PARSING,
            task_name="app.ingestion.tasks.process_discovered_document_task",
            payload={"discovered_dict": {"source_url": "https://example.com/c.pdf"}},
            exc=ValueError("bad magic bytes"),
        )

        fetched = await dlq.get(entry.entry_id)

        assert fetched.entry_id == entry.entry_id
        assert fetched.category == FailureCategory.PDF_PARSING
        assert fetched.error_type == "ValueError"
        assert fetched.error_message == "bad magic bytes"
        assert fetched.status == DLQStatus.PENDING
        assert fetched.traceback is not None

    async def test_get_missing_entry_raises(self, dlq):
        with pytest.raises(DLQEntryNotFoundError):
            await dlq.get("does-not-exist")


@pytest.mark.asyncio
class TestDeadLetterQueueListingAndFiltering:
    async def test_list_all_returns_newest_first(self, dlq):
        first = await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={}, exc=ValueError("a"))
        second = await dlq.send(category=FailureCategory.LLM_EXTRACTION, task_name="t", payload={}, exc=ValueError("b"))

        entries = await dlq.list()

        assert [e.entry_id for e in entries] == [second.entry_id, first.entry_id]

    async def test_filter_by_category(self, dlq):
        await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={}, exc=ValueError("a"))
        target = await dlq.send(category=FailureCategory.MALFORMED_AST, task_name="t", payload={}, exc=ValueError("b"))

        entries = await dlq.list(category=FailureCategory.MALFORMED_AST)

        assert [e.entry_id for e in entries] == [target.entry_id]

    async def test_filter_by_status(self, dlq):
        entry = await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={}, exc=ValueError("a"))
        await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={}, exc=ValueError("b"))
        await dlq.discard(entry.entry_id, discarded_by="admin-1")

        pending = await dlq.list(status=DLQStatus.PENDING)
        discarded = await dlq.list(status=DLQStatus.DISCARDED)

        assert len(pending) == 1
        assert len(discarded) == 1
        assert discarded[0].entry_id == entry.entry_id

    async def test_filter_by_category_and_status_together(self, dlq):
        a = await dlq.send(category=FailureCategory.VECTOR_INGESTION, task_name="t", payload={}, exc=ValueError("a"))
        b = await dlq.send(category=FailureCategory.VECTOR_INGESTION, task_name="t", payload={}, exc=ValueError("b"))
        await dlq.discard(a.entry_id, discarded_by="admin-1")

        pending_vector = await dlq.list(category=FailureCategory.VECTOR_INGESTION, status=DLQStatus.PENDING)

        assert [e.entry_id for e in pending_vector] == [b.entry_id]

    async def test_pagination_limit_and_offset(self, dlq):
        for i in range(5):
            await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={"i": i}, exc=ValueError(str(i)))

        page1 = await dlq.list(limit=2, offset=0)
        page2 = await dlq.list(limit=2, offset=2)

        assert len(page1) == 2
        assert len(page2) == 2
        assert {e.entry_id for e in page1}.isdisjoint({e.entry_id for e in page2})

    async def test_stats_counts_by_category_and_status(self, dlq):
        await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={}, exc=ValueError("a"))
        entry = await dlq.send(category=FailureCategory.LLM_EXTRACTION, task_name="t", payload={}, exc=ValueError("b"))
        await dlq.discard(entry.entry_id, discarded_by="admin-1")

        stats = await dlq.stats()

        assert stats.total == 2
        assert stats.by_category["pdf_parsing"] == 1
        assert stats.by_category["llm_extraction"] == 1
        assert stats.by_status["pending"] == 1
        assert stats.by_status["discarded"] == 1


@pytest.mark.asyncio
class TestDeadLetterQueueLifecycleTransitions:
    async def test_update_payload_edits_the_stored_payload(self, dlq):
        entry = await dlq.send(
            category=FailureCategory.RSS_POLLING, task_name="t", payload={"url": "http://broken"}, exc=ValueError("a"),
        )

        updated = await dlq.update_payload(entry.entry_id, {"url": "http://fixed"})

        assert updated.payload == {"url": "http://fixed"}
        refetched = await dlq.get(entry.entry_id)
        assert refetched.payload == {"url": "http://fixed"}

    async def test_mark_requeued_moves_status_and_records_requeuer(self, dlq):
        entry = await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={}, exc=ValueError("a"))

        updated = await dlq.mark_requeued(entry.entry_id, requeued_by="engineer-1", requeued_task_id="celery-task-123")

        assert updated.status == DLQStatus.REQUEUED
        assert updated.requeued_by == "engineer-1"
        assert updated.requeued_task_id == "celery-task-123"
        assert updated.requeued_at is not None
        pending = await dlq.list(status=DLQStatus.PENDING)
        requeued = await dlq.list(status=DLQStatus.REQUEUED)
        assert pending == []
        assert len(requeued) == 1

    async def test_mark_resolved_from_pending(self, dlq):
        entry = await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={}, exc=ValueError("a"))

        updated = await dlq.mark_resolved(entry.entry_id, resolved_by="engineer-1", notes="fixed manually")

        assert updated.status == DLQStatus.RESOLVED
        assert updated.resolved_by == "engineer-1"
        assert updated.resolution_notes == "fixed manually"

    async def test_discard_from_pending(self, dlq):
        entry = await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={}, exc=ValueError("a"))

        updated = await dlq.discard(entry.entry_id, discarded_by="engineer-1", notes="duplicate")

        assert updated.status == DLQStatus.DISCARDED

    async def test_cannot_discard_an_already_resolved_entry(self, dlq):
        entry = await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={}, exc=ValueError("a"))
        await dlq.mark_resolved(entry.entry_id, resolved_by="engineer-1")

        with pytest.raises(DLQInvalidTransitionError):
            await dlq.discard(entry.entry_id, discarded_by="engineer-2")

    async def test_cannot_edit_payload_of_a_resolved_entry(self, dlq):
        entry = await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={"x": 1}, exc=ValueError("a"))
        await dlq.mark_resolved(entry.entry_id, resolved_by="engineer-1")

        with pytest.raises(DLQInvalidTransitionError):
            await dlq.update_payload(entry.entry_id, {"x": 2})

    async def test_can_requeue_an_already_requeued_entry_again(self, dlq):
        """A requeue that itself fails again should be re-editable/re-
        requeueable -- REQUEUED is not a terminal status."""
        entry = await dlq.send(category=FailureCategory.PDF_PARSING, task_name="t", payload={}, exc=ValueError("a"))
        await dlq.mark_requeued(entry.entry_id, requeued_by="engineer-1", requeued_task_id="task-1")

        updated = await dlq.mark_requeued(entry.entry_id, requeued_by="engineer-1", requeued_task_id="task-2")

        assert updated.requeued_task_id == "task-2"

    async def test_attempt_count_and_timestamps_are_recorded(self, dlq):
        before = dt.datetime.now(dt.timezone.utc)
        entry = await dlq.send(
            category=FailureCategory.LLM_EXTRACTION, task_name="t", payload={}, exc=ValueError("a"), attempt_count=3,
        )
        after = dt.datetime.now(dt.timezone.utc)

        assert entry.attempt_count == 3
        assert before <= entry.first_failed_at <= after
        assert entry.first_failed_at == entry.last_failed_at
