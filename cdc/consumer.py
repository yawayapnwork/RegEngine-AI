#!/usr/bin/env python3
"""Async Python Kafka CDC Consumer Service for RegEngine AI.

Consumes raw Debezium Change-Data-Capture (CDC) change streams from legacy
relational databases (MS SQL Server / Oracle Broking ERPs), normalizes payloads
into standard `TransactionPayload` instances, posts evaluation requests to the
RegEngine FastAPI evaluation engine, and routes unparseable or failing records
into a Dead-Letter Queue (DLQ) alerting loop.

Usage:
  python cdc/consumer.py --bootstrap-servers localhost:9092 --api-url http://localhost:8000
  python cdc/consumer.py --dry-run
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import Any

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer  # type: ignore # pyright: ignore[reportMissingImports]
except ImportError:
    AIOKafkaConsumer = None  # type: ignore
    AIOKafkaProducer = None  # type: ignore
import httpx

# Import CDC Normalizer and DLQ Handler
try:
    from cdc.dlq_handler import CDCDeadLetterHandler
    from cdc.normalizer import LegacyNormalizer, LegacyNormalizerError
except ImportError:
    from dlq_handler import CDCDeadLetterHandler  # type: ignore
    from normalizer import LegacyNormalizer, LegacyNormalizerError  # type: ignore

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("cdc_consumer")


class CDCKafkaConsumerService:
    def __init__(
        self,
        bootstrap_servers: str = "localhost:9092",
        group_id: str = "regengine-cdc-consumer-group",
        topics: list[str] | None = None,
        regengine_api_url: str = "http://localhost:8000",
        dlq_topic: str = "regengine.cdc.dlq",
        max_retries: int = 3,
        dry_run: bool = False,
    ) -> None:
        self.bootstrap_servers = bootstrap_servers
        self.group_id = group_id
        self.topics = topics or [
            "debezium.legacy.dbo.TBL_TRADE_TRANSACTIONS",
            "debezium.legacy.dbo.TBL_CLIENT_COLLATERAL",
        ]
        self.regengine_api_url = regengine_api_url.rstrip("/")
        self.dlq_topic = dlq_topic
        self.max_retries = max_retries
        self.dry_run = dry_run

        self.running = False
        self.processed_count = 0
        self.evaluated_count = 0
        self.dlq_count = 0

        self.consumer: AIOKafkaConsumer | None = None
        self.producer: AIOKafkaProducer | None = None
        self.dlq_handler: CDCDeadLetterHandler | None = None
        self.http_client: httpx.AsyncClient | None = None

    async def initialize(self) -> None:
        logger.info("Initializing CDC Kafka Consumer Service...")
        self.http_client = httpx.AsyncClient(timeout=10.0)

        if self.dry_run:
            logger.info("⚡ Running in DRY-RUN mode — Kafka connection and API calls simulated.")
            self.dlq_handler = CDCDeadLetterHandler(dlq_topic=self.dlq_topic, regengine_api_url=self.regengine_api_url)
            return

        # Initialize Kafka Producer for DLQ forwarding
        try:
            self.producer = AIOKafkaProducer(bootstrap_servers=self.bootstrap_servers)
            await self.producer.start()
            logger.info("AIOKafkaProducer started cleanly connected to %s", self.bootstrap_servers)
        except Exception as exc:
            logger.warning("Could not start Kafka Producer (%s). DLQ producer will run in HTTP-only fallback mode.", exc)
            self.producer = None

        self.dlq_handler = CDCDeadLetterHandler(
            dlq_topic=self.dlq_topic,
            producer=self.producer,
            regengine_api_url=self.regengine_api_url,
        )

        # Initialize Kafka Consumer
        self.consumer = AIOKafkaConsumer(
            *self.topics,
            bootstrap_servers=self.bootstrap_servers,
            group_id=self.group_id,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
        )
        await self.consumer.start()
        logger.info("AIOKafkaConsumer started cleanly listening to topics: %s", self.topics)

    async def shutdown(self) -> None:
        logger.info("Shutting down CDC Consumer Service...")
        self.running = False

        if self.consumer:
            await self.consumer.stop()
            logger.info("Kafka consumer stopped.")

        if self.producer:
            await self.producer.stop()
            logger.info("Kafka producer stopped.")

        if self.http_client:
            await self.http_client.aclose()
            logger.info("HTTP client closed.")

        logger.info(
            "Final Stats: Processed=%d, Evaluated=%d, DLQ_Enqueued=%d",
            self.processed_count, self.evaluated_count, self.dlq_count,
        )

    async def _send_for_evaluation(self, payload: dict[str, Any]) -> bool:
        """Sends normalized TransactionPayload to FastAPI /v1/execution/transactions/evaluate endpoint."""
        url = f"{self.regengine_api_url}/v1/execution/transactions/evaluate"

        if self.dry_run:
            logger.info("DRY-RUN: Simulated evaluation call for transaction '%s'", payload.get("transaction_id"))
            return True

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = await self.http_client.post(url, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info(
                        "✅ Transaction '%s' evaluated: decision=%s, latency=%.1fms",
                        payload.get("transaction_id"),
                        data.get("decision"),
                        data.get("latency_ms", 0.0),
                    )
                    return True
                elif resp.status_code in (500, 502, 503, 504):
                    logger.warning("RegEngine API returned transient status %d (attempt %d/%d)", resp.status_code, attempt, self.max_retries)
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                else:
                    logger.error("RegEngine API rejected payload with status %d: %s", resp.status_code, resp.text)
                    return False
            except Exception as exc:
                logger.warning("HTTP error calling evaluation endpoint (attempt %d/%d): %s", attempt, self.max_retries, exc)
                await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

        return False

    async def process_single_message(self, msg: Any) -> None:
        self.processed_count += 1
        raw_val = msg.value if hasattr(msg, "value") else msg

        # 1. Parse JSON Record
        try:
            if isinstance(raw_val, (bytes, bytearray)):
                raw_json = json.loads(raw_val.decode("utf-8"))
            elif isinstance(raw_val, str):
                raw_json = json.loads(raw_val)
            elif isinstance(raw_val, dict):
                raw_json = raw_val
            else:
                raise LegacyNormalizerError(f"Unsupported record value type: {type(raw_val)}")
        except Exception as json_exc:
            self.dlq_count += 1
            await self.dlq_handler.handle_failed_record(
                raw_message=raw_val,
                error=json_exc,
                topic=getattr(msg, "topic", "unknown"),
                partition=getattr(msg, "partition", 0),
                offset=getattr(msg, "offset", 0),
                context="corrupted_json_parsing",
            )
            return

        # 2. Normalize Legacy DB Payload
        try:
            normalized_tx = LegacyNormalizer.normalize_event(raw_json)
        except Exception as norm_exc:
            self.dlq_count += 1
            await self.dlq_handler.handle_failed_record(
                raw_message=raw_json,
                error=norm_exc,
                topic=getattr(msg, "topic", "unknown"),
                partition=getattr(msg, "partition", 0),
                offset=getattr(msg, "offset", 0),
                context="legacy_normalization_failure",
            )
            return

        if normalized_tx is None:
            # Delete event ignored
            return

        # 3. Post to RegEngine FastAPI Endpoint for Rule Evaluation
        payload_dict = normalized_tx.model_dump(mode="json")
        success = await self._send_for_evaluation(payload_dict)

        if success:
            self.evaluated_count += 1
        else:
            self.dlq_count += 1
            await self.dlq_handler.handle_failed_record(
                raw_message=payload_dict,
                error=RuntimeError(f"RegEngine evaluation API unreachable or failed after {self.max_retries} attempts"),
                topic=getattr(msg, "topic", "unknown"),
                partition=getattr(msg, "partition", 0),
                offset=getattr(msg, "offset", 0),
                context="api_evaluation_exhausted",
            )

    async def run(self) -> None:
        await self.initialize()
        self.running = True

        if self.dry_run:
            logger.info("Executing dry-run message processing loop simulation...")
            sample_legacy_trade = {
                "op": "c",
                "source": {"table": "TBL_TRADE_TRANSACTIONS"},
                "after": {
                    "N_TRANS_ID": 99001,
                    "VC_BROKER_CODE": "BROKER_001",
                    "VC_CLIENT_CODE": "CLI_DRYRUN_1",
                    "N_CLIENT_CAT": 1,
                    "VC_SYMBOL": "RELIANCE",
                    "N_ORDER_QTY": 100,
                    "N_ORDER_PRICE_Paisa": 250000,
                    "N_UPFRONT_MARGIN_BP": 2000,
                    "N_PEAK_MARGIN_FLAG": 1,
                    "VC_SEGMENT_TYPE": "CASH",
                    "DT_TRADE_TIME": "20260828233500",
                },
            }
            sample_malformed = {"op": "c", "after": {"INVALID_FIELD": "NO_PK"}}

            await self.process_single_message(sample_legacy_trade)
            await self.process_single_message(sample_malformed)
            await self.shutdown()
            return

        logger.info("Entering Kafka message consumption loop...")
        try:
            while self.running:
                data = await self.consumer.getmany(timeout_ms=1000, max_records=50)
                for tp, messages in data.items():
                    for message in messages:
                        await self.process_single_message(message)
                    await self.consumer.commit({tp: messages[-1].offset + 1})
        except asyncio.CancelledError:
            logger.info("Consumer loop task cancelled.")
        except Exception as exc:
            logger.error("Error in consumer loop: %s", exc, exc_info=True)
        finally:
            await self.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Async Python Kafka CDC Consumer Service")
    parser.add_argument("--bootstrap-servers", default=os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092"), help="Kafka bootstrap servers")
    parser.add_argument("--group-id", default=os.getenv("KAFKA_GROUP_ID", "regengine-cdc-consumer-group"), help="Kafka consumer group ID")
    parser.add_argument("--api-url", default=os.getenv("REGENGINE_API_URL", "http://localhost:8000"), help="RegEngine AI API base URL")
    parser.add_argument("--topics", nargs="+", default=None, help="Kafka topics to subscribe to")
    parser.add_argument("--dry-run", action="store_true", help="Simulate consumer execution without connecting to Kafka")

    args = parser.parse_args()

    service = CDCKafkaConsumerService(
        bootstrap_servers=args.bootstrap_servers,
        group_id=args.group_id,
        topics=args.topics,
        regengine_api_url=args.api_url,
        dry_run=args.dry_run,
    )

    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        logger.info("Stopped by user.")


if __name__ == "__main__":
    main()
