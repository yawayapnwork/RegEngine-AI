"""Dead-Letter Queue & Operator Alerting Loop for CDC Pipeline.

Captures unparseable legacy database messages, schema mismatches, corrupted JSON records,
or persistent RegEngine API evaluation failures.

Actions:
  1. Serializes diagnostic failure envelope with error cause, stack traceback, raw payload, and partition metadata.
  2. Publishes failed record to Kafka Dead-Letter Queue topic (`regengine.cdc.dlq`).
  3. Posts alert context to RegEngine AI administrative DLQ API (`/v1/admin/dlq` or `/hitl/cases`) to notify compliance operators.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import traceback
from typing import Any
try:
    from aiokafka import AIOKafkaProducer  # type: ignore # pyright: ignore[reportMissingImports]
except ImportError:
    AIOKafkaProducer = None  # type: ignore
import httpx

logger = logging.getLogger("cdc_dlq_handler")


class CDCDeadLetterHandler:
    def __init__(
        self,
        dlq_topic: str = "regengine.cdc.dlq",
        producer: AIOKafkaProducer | None = None,
        regengine_api_url: str = "http://localhost:8000",
        admin_jwt_token: str | None = None,
    ) -> None:
        self.dlq_topic = dlq_topic
        self.producer = producer
        self.regengine_api_url = regengine_api_url.rstrip("/")
        self.admin_jwt_token = admin_jwt_token

    async def handle_failed_record(
        self,
        raw_message: bytes | str | dict[str, Any],
        error: Exception,
        topic: str = "unknown_topic",
        partition: int = 0,
        offset: int = 0,
        context: str = "parsing_failure",
    ) -> dict[str, Any]:
        """Constructs DLQ diagnostic record, publishes to Kafka DLQ topic, and alerts compliance operators."""
        timestamp_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        error_msg = str(error)
        tb_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))

        raw_payload_str: str
        if isinstance(raw_message, bytes):
            try:
                raw_payload_str = raw_message.decode("utf-8")
            except UnicodeDecodeError:
                raw_payload_str = str(raw_message)
        elif isinstance(raw_message, dict):
            raw_payload_str = json.dumps(raw_message, default=str)
        else:
            raw_payload_str = str(raw_message)

        dlq_record = {
            "failure_id": f"dlq-cdc-{partition}-{offset}-{int(dt.datetime.now().timestamp())}",
            "source_topic": topic,
            "partition": partition,
            "offset": offset,
            "context": context,
            "error_type": type(error).__name__,
            "error_message": error_msg,
            "stack_trace": tb_str,
            "failed_at": timestamp_iso,
            "raw_payload": raw_payload_str,
        }

        logger.error(
            "🚨 CDC DLQ EVENT CAPTURED [topic=%s partition=%d offset=%d context=%s]: %s",
            topic, partition, offset, context, error_msg,
        )

        # 1. Publish to Kafka DLQ Topic
        if self.producer:
            try:
                msg_bytes = json.dumps(dlq_record).encode("utf-8")
                await self.producer.send_and_wait(self.dlq_topic, msg_bytes)
                logger.info("Published failed record to Kafka DLQ topic '%s'", self.dlq_topic)
            except Exception as kafka_exc:
                logger.warning("Failed to publish to Kafka DLQ topic '%s': %s", self.dlq_topic, kafka_exc)

        # 2. Alert RegEngine Compliance Operators via Administrative DLQ API
        await self._alert_compliance_operators(dlq_record)

        return dlq_record

    async def _alert_compliance_operators(self, dlq_record: dict[str, Any]) -> None:
        """Sends HTTP alert payload to RegEngine AI admin DLQ / HITL review routes."""
        headers = {"Content-Type": "application/json"}
        if self.admin_jwt_token:
            headers["Authorization"] = f"Bearer {self.admin_jwt_token}"

        alert_payload = {
            "task_name": f"cdc.ingestion.{dlq_record['context']}",
            "category": "cdc_unparseable_format",
            "error_type": dlq_record["error_type"],
            "error_message": dlq_record["error_message"],
            "payload": dlq_record,
            "source": f"Kafka topic: {dlq_record['source_topic']} (p:{dlq_record['partition']} o:{dlq_record['offset']})",
        }

        url = f"{self.regengine_api_url}/v1/admin/dlq"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=alert_payload, headers=headers)
                if resp.status_code in (200, 201, 202):
                    logger.info("✅ Alert posted to RegEngine Admin DLQ endpoint successfully.")
                else:
                    logger.warning("RegEngine DLQ Alert API returned %d: %s", resp.status_code, resp.text)
        except Exception as exc:
            logger.warning("Could not send HTTP alert to RegEngine API (%s): %s", url, exc)
