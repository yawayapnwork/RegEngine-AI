#!/usr/bin/env python3
"""Kafka producer for `regengine.trades.raw` -- publishes live or
synthetic order/trade events for the PyFlink compliance stream to
consume.

Usage:
  python flink/kafka_trade_producer.py --bootstrap localhost:29092 --count 500 --rate 200
  python flink/kafka_trade_producer.py --bootstrap localhost:29092 --file trades.jsonl
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flink.kafka_config import RAW_TRADES_TOPIC, producer_config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("kafka_trade_producer")

try:
    from confluent_kafka import Producer

    CONFLUENT_KAFKA_AVAILABLE = True
except ImportError:
    CONFLUENT_KAFKA_AVAILABLE = False


def _delivery_callback(err, msg) -> None:
    if err is not None:
        logger.error("Delivery failed for key=%s: %s", msg.key(), err)


def synthetic_trade_event(seq: int) -> dict:
    broker_id = f"INZ{random.randint(1001, 1005):07d}"
    client_code = f"CLI_{random.randint(101, 110)}"
    order_val = random.randint(100_000, 5_000_000)
    upfront_pct = round(random.uniform(12.0, 35.0), 2)
    return {
        "transaction_id": f"STREAM-TX-{seq:08d}",
        "broker_id": broker_id,
        "entity_type": "Stockbroker",
        "timestamp_ms": int(time.time() * 1000),
        "facts": {
            "client_code": client_code,
            "upfront_margin_pct": upfront_pct,
            "peak_margin_collected": bool(random.random() > 0.1),
            "client_funds_segregated": bool(random.random() > 0.05),
            "order_value_inr": order_val,
        },
    }


def run_synthetic(bootstrap: str, count: int, rate_per_sec: float) -> None:
    if not CONFLUENT_KAFKA_AVAILABLE:
        raise RuntimeError("confluent-kafka is required: pip install -r flink/requirements.txt")

    producer = Producer(producer_config(bootstrap))
    delay = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0

    for i in range(1, count + 1):
        event = synthetic_trade_event(i)
        key = f"{event['broker_id']}:{event['facts']['client_code']}".encode("utf-8")
        producer.produce(
            topic=RAW_TRADES_TOPIC,
            key=key,
            value=json.dumps(event).encode("utf-8"),
            callback=_delivery_callback,
        )
        producer.poll(0)  # serve delivery callbacks without blocking the send path
        if i % 100 == 0:
            logger.info("Produced %d/%d trade events to %s", i, count, RAW_TRADES_TOPIC)
        if delay:
            time.sleep(delay)

    logger.info("Flushing remaining in-flight messages...")
    producer.flush(timeout=30)
    logger.info("Done. Produced %d trade events to %s.", count, RAW_TRADES_TOPIC)


def run_from_file(bootstrap: str, path: Path) -> None:
    if not CONFLUENT_KAFKA_AVAILABLE:
        raise RuntimeError("confluent-kafka is required: pip install -r flink/requirements.txt")

    producer = Producer(producer_config(bootstrap))
    sent = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            event.setdefault("timestamp_ms", int(time.time() * 1000))
            key = f"{event.get('broker_id', 'unknown')}:{event.get('facts', {}).get('client_code', 'unknown')}".encode("utf-8")
            producer.produce(
                topic=RAW_TRADES_TOPIC,
                key=key,
                value=json.dumps(event).encode("utf-8"),
                callback=_delivery_callback,
            )
            producer.poll(0)
            sent += 1
    producer.flush(timeout=30)
    logger.info("Done. Produced %d trade events from %s to %s.", sent, path, RAW_TRADES_TOPIC)


def main() -> None:
    parser = argparse.ArgumentParser(description="RegEngine AI Kafka trade-event producer")
    parser.add_argument("--bootstrap", default="localhost:29092", help="Kafka bootstrap servers (host listener)")
    parser.add_argument("--count", type=int, default=200, help="Number of synthetic events to produce")
    parser.add_argument("--rate", type=float, default=100.0, help="Target events/sec for synthetic mode")
    parser.add_argument("--file", type=Path, default=None, help="Optional JSONL file of trade events to replay instead of generating synthetic ones")
    args = parser.parse_args()

    if args.file:
        run_from_file(args.bootstrap, args.file)
    else:
        run_synthetic(args.bootstrap, args.count, args.rate)


if __name__ == "__main__":
    main()
