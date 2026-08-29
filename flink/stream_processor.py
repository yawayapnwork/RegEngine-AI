"""PyFlink High-Frequency Event-Stream Execution Engine for RegEngine AI.

Builds a real-time stream processing topology on Apache Flink (PyFlink
DataStream API) that evaluates live SEBI market trade/order events from
Kafka in <5ms and emits a compliance decision per event.

Topology:

    Kafka(regengine.trades.raw)
        -> WatermarkStrategy (bounded-out-of-orderness, 5s)
        -> keyBy(broker_id, client_code)
        -> SlidingMarginWindowFunction   (KeyedProcessFunction + RocksDB-backed ListState)
        -> OPADecisionFunction           (RichMapFunction, sync httpx to embedded OPA sidecar)
        -> Kafka(regengine.trades.evaluated)   [EXACTLY_ONCE sink]

Why a KeyedProcessFunction instead of Flink's built-in
`SlidingEventTimeWindows` for the margin aggregation: the built-in window
API only emits a result when a window *fires* (at its boundary), which is
wrong for this use case -- a compliance decision must be produced for
*every single trade*, using the client's cumulative margin/order value as
of that trade. `SlidingMarginWindowFunction` reimplements the sliding
24h-lookback semantics as incremental keyed state (evict-then-append,
O(entries currently in window) per record) so every event gets an
up-to-date aggregate synchronously, which native windows cannot do.

Why OPA is queried per-record via a synchronous RichMapFunction rather
than an async client: PyFlink Python UDFs execute on the operator's task
thread one record at a time -- there is no per-record async hook in the
DataStream API. A persistent `httpx.Client` opened once in `open()` and
reused across the operator's lifetime keeps each call to the co-located
OPA sidecar in the low single-digit-millisecond range (loopback HTTP,
compiled policy resident in memory), which is what keeps the pipeline
inside its <5ms budget. Job parallelism (task slots) is what gives you
throughput, not per-record concurrency inside a single subtask.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any, Iterator

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.execution.models import Decision

logger = logging.getLogger("flink_stream_processor")

try:
    from pyflink.common import Duration, WatermarkStrategy
    from pyflink.common.serialization import SimpleStringSchema
    from pyflink.common.typeinfo import Types
    from pyflink.datastream import CheckpointingMode, RuntimeContext, StreamExecutionEnvironment
    from pyflink.datastream.connectors.kafka import (
        DeliveryGuarantee,
        KafkaOffsetsInitializer,
        KafkaRecordSerializationSchema,
        KafkaSink,
        KafkaSource,
    )
    from pyflink.datastream.functions import KeyedProcessFunction, RichMapFunction
    from pyflink.datastream.state import EmbeddedRocksDBStateBackend, ListStateDescriptor, StateTtlConfig

    PYFLINK_AVAILABLE = True
except ImportError:
    PYFLINK_AVAILABLE = False
    logger.warning("PyFlink module not installed locally. Operating in standalone simulation mode.")

    # Allow KeyedProcessFunction / RichMapFunction to be subclassed below even
    # when pyflink isn't installed (e.g. running flink_job_runner.py --dry-run
    # on a laptop without the JVM toolchain).
    class KeyedProcessFunction:  # type: ignore[no-redef]
        pass

    class RichMapFunction:  # type: ignore[no-redef]
        pass


import httpx

RAW_TRADES_TOPIC = "regengine.trades.raw"
EVALUATED_DECISIONS_TOPIC = "regengine.trades.evaluated"
SLIDING_WINDOW_SECONDS = 24 * 3600  # daily cumulative margin lookback
ALLOWED_LATENESS_SECONDS = 5


class SlidingMarginWindowFunction(KeyedProcessFunction):
    """Keyed on `f"{broker_id}:{client_code}"`. Maintains a RocksDB-backed
    `ListState` of `(event_time_ms, order_value_inr, margin_collected_inr)`
    entries for the trailing `SLIDING_WINDOW_SECONDS`. On every element:
    evicts entries that have aged out of the window, appends the new
    trade, sums what remains, and emits the original event enriched with
    the cumulative facts. An event-time timer registered just past the
    oldest surviving entry's expiry guarantees state is pruned even for
    keys that go quiet (no new trades to trigger eviction).
    """

    def __init__(self, window_seconds: int = SLIDING_WINDOW_SECONDS) -> None:
        self.window_seconds = window_seconds
        self.window_ms = window_seconds * 1000
        self._entries_state = None

    def open(self, runtime_context: "RuntimeContext") -> None:
        descriptor = ListStateDescriptor("margin_window_entries", Types.PICKLED_BYTE_ARRAY())
        self._entries_state = runtime_context.get_list_state(descriptor)

    def process_element(self, value: str, ctx: "KeyedProcessFunction.Context") -> Iterator[str]:
        event = json.loads(value)
        event_time_ms = int(ctx.timestamp())
        cutoff_ms = event_time_ms - self.window_ms

        facts = event.get("facts", {})
        order_value = float(facts.get("order_value_inr", 0.0))
        upfront_pct = float(facts.get("upfront_margin_pct", 0.0))
        margin_collected = order_value * upfront_pct / 100.0

        surviving = [
            e for e in (self._entries_state.get() or []) if e[0] >= cutoff_ms
        ]
        surviving.append((event_time_ms, order_value, margin_collected))
        self._entries_state.update(surviving)

        # Prune-on-idle: fire once just after the current entry would age out,
        # so keys with no further trades don't hold RocksDB state forever.
        ctx.timer_service().register_event_time_timer(event_time_ms + self.window_ms + 1)

        cumulative_order_value = sum(e[1] for e in surviving)
        cumulative_margin = sum(e[2] for e in surviving)

        event["window_state"] = {
            "cumulative_order_value_inr": round(cumulative_order_value, 2),
            "cumulative_margin_collected_inr": round(cumulative_margin, 2),
            "trade_count": len(surviving),
            "window_seconds": self.window_seconds,
        }
        yield json.dumps(event)

    def on_timer(self, timestamp: int, ctx: "KeyedProcessFunction.OnTimerContext") -> Iterator[str]:
        cutoff_ms = timestamp - self.window_ms
        surviving = [e for e in (self._entries_state.get() or []) if e[0] >= cutoff_ms]
        if surviving:
            self._entries_state.update(surviving)
        else:
            self._entries_state.clear()
        return iter(())


class OPADecisionFunction(RichMapFunction):
    """Synchronous per-record OPA query against the co-located sidecar.
    Package convention mirrors `app.execution.opa_engine`:
    `data.regengine.<entity_type_lower>.decision`.
    """

    def __init__(self, opa_url: str = "http://localhost:8181", timeout_seconds: float = 2.0) -> None:
        self.opa_url = opa_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._client: httpx.Client | None = None
        self._opa_offline = False

    def open(self, runtime_context: "RuntimeContext") -> None:
        self._client = httpx.Client(timeout=self.timeout_seconds)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def map(self, value: str) -> str:
        t0 = time.perf_counter()
        event = json.loads(value)
        entity_type = event.get("entity_type", "Stockbroker")
        window_state = event.get("window_state", {})
        facts = dict(event.get("facts", {}))

        facts["cumulative_margin_collected_inr"] = window_state.get("cumulative_margin_collected_inr", 0.0)
        facts["cumulative_order_value_inr"] = window_state.get("cumulative_order_value_inr", 0.0)
        facts["intraday_trade_count"] = window_state.get("trade_count", 1)
        cum_order_val = max(1.0, facts["cumulative_order_value_inr"])
        facts["effective_cumulative_margin_pct"] = (facts["cumulative_margin_collected_inr"] / cum_order_val) * 100.0

        decision, reasons = self._query_opa(entity_type, facts)
        latency_ms = (time.perf_counter() - t0) * 1000

        result = {
            "transaction_id": event.get("transaction_id", "unknown"),
            "broker_id": event.get("broker_id", "unknown"),
            "entity_type": entity_type,
            "decision": decision,
            "reasons": reasons,
            "latency_ms": round(latency_ms, 2),
            "window_state": window_state,
            "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        return json.dumps(result)

    def _query_opa(self, entity_type: str, facts: dict[str, Any]) -> tuple[str, list[str]]:
        if not self._opa_offline:
            try:
                package = f"regengine/{entity_type.lower()}"
                url = f"{self.opa_url}/v1/data/{package}/decision"
                resp = self._client.post(url, json={"input": {"entity_type": entity_type, "facts": facts}})
                if resp.status_code == 200:
                    result = resp.json().get("result") or {}
                    violations = list(result.get("violations", []) or [])
                    return (Decision.DENY.value if violations else Decision.ALLOW.value), violations
                return Decision.FLAGGED.value, [f"OPA server status {resp.status_code}"]
            except httpx.HTTPError:
                logger.warning("OPA sidecar unreachable; falling back to in-memory deterministic evaluation.")
                self._opa_offline = True

        upfront_pct = facts.get("upfront_margin_pct", 20.0)
        segregated = facts.get("client_funds_segregated", True)
        if upfront_pct < 20.0 or not segregated:
            return Decision.DENY.value, ["Upfront margin below 20% or unsegregated client funds."]
        return Decision.ALLOW.value, []


def build_pyflink_topology(
    kafka_bootstrap: str = "localhost:9092",
    opa_url: str = "http://localhost:8181",
    checkpoint_dir: str = "file:///tmp/regengine-flink-checkpoints",
) -> "StreamExecutionEnvironment":
    """Assembles the full PyFlink DataStream topology: Kafka source ->
    watermarking -> keyed sliding-window margin aggregation -> OPA
    decisioning -> Kafka sink, with RocksDB state + exactly-once
    checkpointing end to end (source offsets, operator state, and sink
    writes are all covered by the same checkpoint barrier)."""
    if not PYFLINK_AVAILABLE:
        raise RuntimeError("PyFlink package ('apache-flink') is required to construct the live PyFlink topology.")

    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(4)

    # 1. Fault tolerance: RocksDB state backend + exactly-once checkpointing.
    env.set_state_backend(EmbeddedRocksDBStateBackend())
    env.get_checkpoint_config().set_checkpointing_mode(CheckpointingMode.EXACTLY_ONCE)
    env.get_checkpoint_config().set_checkpoint_interval(5000)  # 5s
    env.get_checkpoint_config().set_min_pause_between_checkpoints(1000)
    env.get_checkpoint_config().set_checkpoint_timeout(60000)
    env.get_checkpoint_config().set_max_concurrent_checkpoints(1)
    env.set_default_savepoint_directory(checkpoint_dir)

    # 2. Event-time watermarking: 5s bounded out-of-orderness.
    watermark_strategy = WatermarkStrategy.for_bounded_out_of_orderness(
        Duration.of_seconds(ALLOWED_LATENESS_SECONDS)
    ).with_timestamp_assigner(lambda event, _ts: json.loads(event).get("timestamp_ms", int(time.time() * 1000)))

    # 3. Kafka source: exactly-once reads via checkpointed offsets (no
    # auto-commit -- offsets are only advanced when a checkpoint completes).
    kafka_source = (
        KafkaSource.builder()
        .set_bootstrap_servers(kafka_bootstrap)
        .set_topics(RAW_TRADES_TOPIC)
        .set_group_id("regengine-pyflink-eval-group")
        .set_starting_offsets(KafkaOffsetsInitializer.committed_offsets())
        .set_value_only_deserializer(SimpleStringSchema())
        .set_property("isolation.level", "read_committed")
        .build()
    )
    raw_stream = env.from_source(kafka_source, watermark_strategy, "Kafka_Raw_Trades_Source")

    # 4. Keyed stateful sliding-window margin aggregation.
    keyed_stream = raw_stream.key_by(
        lambda value: (lambda e: f"{e.get('broker_id', 'unknown')}:{e.get('facts', {}).get('client_code', 'unknown')}")(
            json.loads(value)
        ),
        key_type=Types.STRING(),
    )
    enriched_stream = keyed_stream.process(SlidingMarginWindowFunction(), output_type=Types.STRING())

    # 5. Embedded OPA decisioning (<5ms per record against the sidecar).
    decision_stream = enriched_stream.map(OPADecisionFunction(opa_url=opa_url), output_type=Types.STRING())

    # 6. Kafka sink: exactly-once via Kafka transactions.
    kafka_sink = (
        KafkaSink.builder()
        .set_bootstrap_servers(kafka_bootstrap)
        .set_record_serializer(
            KafkaRecordSerializationSchema.builder()
            .set_topic(EVALUATED_DECISIONS_TOPIC)
            .set_value_serialization_schema(SimpleStringSchema())
            .build()
        )
        .set_delivery_guarantee(DeliveryGuarantee.EXACTLY_ONCE)
        .set_transactional_id_prefix("regengine-flink-decisions-")
        .set_property("transaction.timeout.ms", "900000")
        .build()
    )
    decision_stream.sink_to(kafka_sink).name("Kafka_Evaluated_Decisions_Sink")

    logger.info(
        "PyFlink event-stream topology built: %s -> sliding-window(%ss) -> OPA -> %s",
        RAW_TRADES_TOPIC, SLIDING_WINDOW_SECONDS, EVALUATED_DECISIONS_TOPIC,
    )
    return env


# --- Standalone (non-Flink) evaluator, reused by flink/flink_job_runner.py's
# --dry-run simulator so the OPA-query logic under test is identical to what
# the real PyFlink job runs -- only the state/windowing plumbing differs
# (a plain dict there vs. RocksDB ListState here). ---
class OPAStreamEvaluator:
    """Async twin of `OPADecisionFunction` for use outside a Flink runtime
    (dry-run simulation, unit tests)."""

    def __init__(self, opa_url: str = "http://localhost:8181", timeout: float = 2.0) -> None:
        self.opa_url = opa_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._opa_offline: bool = False

    async def evaluate_trade(
        self,
        trade_payload: dict[str, Any],
        window_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Queries the OPA policy engine with combined trade facts and
        cumulative sliding-window facts."""
        t0 = time.perf_counter()
        entity_type = trade_payload.get("entity_type", "Stockbroker")
        facts = dict(trade_payload.get("facts", {}))

        facts["cumulative_margin_collected_inr"] = window_state.get("cumulative_margin_collected_inr", 0.0)
        facts["cumulative_order_value_inr"] = window_state.get("cumulative_order_value_inr", 0.0)
        facts["intraday_trade_count"] = window_state.get("trade_count", 1)

        cum_order_val = max(1.0, facts["cumulative_order_value_inr"])
        facts["effective_cumulative_margin_pct"] = (facts["cumulative_margin_collected_inr"] / cum_order_val) * 100.0

        if not self._opa_offline:
            package = f"regengine/{entity_type.lower()}"
            url = f"{self.opa_url}/v1/data/{package}/decision"
            input_doc = {"input": {"entity_type": entity_type, "facts": facts}}

            try:
                if self._client is None:
                    self._client = httpx.AsyncClient(timeout=self.timeout)
                resp = await self._client.post(url, json=input_doc)
                latency_ms = (time.perf_counter() - t0) * 1000

                if resp.status_code == 200:
                    result = resp.json().get("result") or {}
                    violations = list(result.get("violations", []) or [])
                    decision = Decision.DENY.value if violations else Decision.ALLOW.value
                    reasons = violations
                else:
                    decision = Decision.FLAGGED.value
                    reasons = [f"OPA server status {resp.status_code}"]
                return {
                    "transaction_id": trade_payload.get("transaction_id", "unknown"),
                    "broker_id": trade_payload.get("broker_id", "unknown"),
                    "entity_type": entity_type,
                    "decision": decision,
                    "reasons": reasons,
                    "latency_ms": round(latency_ms, 2),
                    "window_state": window_state,
                    "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                }
            except Exception:
                self._opa_offline = True  # Fast-path fallback for dry-run simulation

        upfront_pct = facts.get("upfront_margin_pct", 20.0)
        segregated = facts.get("client_funds_segregated", True)
        if upfront_pct < 20.0 or not segregated:
            decision = Decision.DENY.value
            reasons = ["Upfront margin below 20% or unsegregated client funds."]
        else:
            decision = Decision.ALLOW.value
            reasons = []

        latency_ms = (time.perf_counter() - t0) * 1000
        return {
            "transaction_id": trade_payload.get("transaction_id", "unknown"),
            "broker_id": trade_payload.get("broker_id", "unknown"),
            "entity_type": entity_type,
            "decision": decision,
            "reasons": reasons,
            "latency_ms": round(latency_ms, 2),
            "window_state": window_state,
            "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
