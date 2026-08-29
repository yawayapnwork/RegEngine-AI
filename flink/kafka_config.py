"""Shared Kafka producer/consumer configuration for the RegEngine AI
high-frequency compliance stream.

Centralized here so the trade-event producer (`kafka_trade_producer.py`),
the PyFlink job's Kafka source/sink (`stream_processor.py`), and any
downstream consumer of `regengine.trades.evaluated` all agree on the same
delivery-guarantee knobs -- a mismatch here (e.g. a consumer not reading
`read_committed`) would silently reintroduce duplicate/uncommitted reads
even though the Flink job itself is exactly-once end to end.
"""
from __future__ import annotations

RAW_TRADES_TOPIC = "regengine.trades.raw"
EVALUATED_DECISIONS_TOPIC = "regengine.trades.evaluated"


def producer_config(bootstrap_servers: str = "localhost:9092") -> dict:
    """High-frequency, duplicate-safe producer config for `regengine.trades.raw`.

    - `enable.idempotence=True` + `acks=all` + bounded in-flight requests:
      Kafka's idempotent-producer protocol de-duplicates retried sends on
      the broker side, so a network retry can never double-publish a trade.
    - `linger.ms`/`batch.size`: small deliberate batching window -- trades
      are high-frequency but the sub-5ms compliance SLA is measured from
      Flink's Kafka source onward, not from producer send, so a few ms of
      client-side batching for throughput is free.
    - `compression.type=lz4`: cheap CPU cost, meaningful bandwidth win on
      JSON trade payloads.
    """
    return {
        "bootstrap.servers": bootstrap_servers,
        "acks": "all",
        "enable.idempotence": True,
        "max.in.flight.requests.per.connection": 5,  # safe with idempotence enabled (<=5)
        "retries": 10,
        "retry.backoff.ms": 100,
        "linger.ms": 5,
        "batch.size": 32768,
        "compression.type": "lz4",
        "client.id": "regengine-trade-producer",
    }


def consumer_config(bootstrap_servers: str = "localhost:9092", group_id: str = "regengine-decisions-consumer") -> dict:
    """Config for a downstream reader of `regengine.trades.evaluated`
    (e.g. the ledger writer or webhook dispatcher).

    - `isolation.level=read_committed`: the Flink sink writes decisions
      transactionally (EXACTLY_ONCE); a consumer on `read_uncommitted`
      would see -- and could act on -- records from an aborted/rolled-back
      checkpoint. This must always be `read_committed` to keep the
      exactly-once guarantee meaningful end to end.
    - `enable.auto.commit=False`: offsets are committed only after the
      record has been durably handled downstream (ledger write / webhook
      ack), not just after it was read off the socket.
    """
    return {
        "bootstrap.servers": bootstrap_servers,
        "group.id": group_id,
        "isolation.level": "read_committed",
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
        "max.poll.records": 500,
        "session.timeout.ms": 45000,
    }


def flink_source_kafka_properties() -> dict:
    """Extra `KafkaSource` properties layered on top of what
    `stream_processor.build_pyflink_topology` sets directly (bootstrap
    servers, topic, group id, deserializer)."""
    return {
        "isolation.level": "read_committed",
        "fetch.min.bytes": "1",
        "fetch.max.wait.ms": "50",
    }


def flink_sink_kafka_properties() -> dict:
    """Extra `KafkaSink` properties layered on top of what
    `stream_processor.build_pyflink_topology` sets directly (delivery
    guarantee, transactional id prefix)."""
    return {
        "transaction.timeout.ms": "900000",  # must exceed checkpoint interval by a wide margin
        "compression.type": "lz4",
    }
