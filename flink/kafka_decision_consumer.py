#!/usr/bin/env python3
"""Kafka consumer for `regengine.trades.evaluated` -- the compliance
decisions the PyFlink job emits. Stands in for whatever durably handles a
decision downstream (audit ledger write, HITL enqueue on FLAGGED, webhook
dispatch on DENY); here it just logs, but the commit discipline is the
part that matters and is meant to be copied as-is.

Usage:
  python flink/kafka_decision_consumer.py --bootstrap localhost:29092
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flink.kafka_config import EVALUATED_DECISIONS_TOPIC, consumer_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("kafka_decision_consumer")

try:
    from confluent_kafka import Consumer

    CONFLUENT_KAFKA_AVAILABLE = True
except ImportError:
    CONFLUENT_KAFKA_AVAILABLE = False


def handle_decision(decision: dict) -> None:
    """Placeholder for the durable side effect (ledger write / webhook /
    HITL enqueue). Must complete -- or raise -- before the offset for this
    record is committed, so a crash here replays the record instead of
    silently dropping it."""
    logger.info(
        "decision=%s transaction_id=%s broker_id=%s latency_ms=%.2f reasons=%s",
        decision.get("decision"),
        decision.get("transaction_id"),
        decision.get("broker_id"),
        decision.get("latency_ms", 0.0),
        decision.get("reasons"),
    )


def run(bootstrap: str, group_id: str) -> None:
    if not CONFLUENT_KAFKA_AVAILABLE:
        raise RuntimeError("confluent-kafka is required: pip install -r flink/requirements.txt")

    consumer = Consumer(consumer_config(bootstrap, group_id))
    consumer.subscribe([EVALUATED_DECISIONS_TOPIC])
    logger.info("Subscribed to %s (group=%s, isolation.level=read_committed)", EVALUATED_DECISIONS_TOPIC, group_id)

    try:
        while True:
            msg = consumer.poll(timeout=1.0)
            if msg is None:
                continue
            if msg.error():
                logger.error("Consumer error: %s", msg.error())
                continue

            decision = json.loads(msg.value())
            try:
                handle_decision(decision)
                # Manual commit only after the downstream side effect
                # succeeded -- see consumer_config()'s enable.auto.commit=False.
                consumer.commit(message=msg, asynchronous=False)
            except Exception:
                logger.exception(
                    "Failed to handle decision transaction_id=%s; offset will not be committed and will be redelivered.",
                    decision.get("transaction_id"),
                )
    except KeyboardInterrupt:
        logger.info("Shutting down consumer.")
    finally:
        consumer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="RegEngine AI Kafka decision consumer")
    parser.add_argument("--bootstrap", default="localhost:29092", help="Kafka bootstrap servers (host listener)")
    parser.add_argument("--group-id", default="regengine-decisions-consumer", help="Consumer group id")
    args = parser.parse_args()
    run(args.bootstrap, args.group_id)


if __name__ == "__main__":
    main()
