"""Application configuration.

All tunables are sourced from environment variables (via .env in local dev)
so the service can be promoted across environments without code changes.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")

    # --- Service ---
    service_name: str = "sebi-circular-parser"
    max_upload_mb: int = Field(default=50, description="Max accepted PDF size in MB")
    parse_timeout_seconds: int = Field(default=180, description="Hard timeout for a single parse job")
    parse_concurrency: int = Field(default=4, description="Max PDFs processed concurrently by this instance")

    # --- Extraction backend ---
    # "unstructured" (hi_res, layout aware) or "tika" (fallback, faster, weaker layout fidelity)
    extraction_backend: str = "unstructured"
    unstructured_strategy: str = "hi_res"  # hi_res | fast | ocr_only
    tika_server_url: str = "http://localhost:9998"

    # --- Chunking ---
    chunk_max_chars: int = 2400
    chunk_overlap_chars: int = 200
    chunk_min_chars: int = 40

    # --- Embeddings ---
    embedding_model_name: str = "BAAI/bge-large-en-v1.5"
    embedding_dim: int = 1024
    embedding_batch_size: int = 16

    # --- Qdrant ---
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_collection: str = "sebi_master_circulars"
    qdrant_upsert_batch_size: int = 64
    qdrant_timeout_seconds: float = 30.0

    # --- Compliance rule extraction (CrewAI dual-agent pipeline) ---
    anthropic_api_key: str | None = None
    agent_verbose: bool = False
    agent_max_rpm: int = 20

    # --- Execution service: embedded OPA engine ---
    # OPA runs as a local/sidecar `opa run --server` process. Policies are
    # pushed via its REST Policy API (PUT /v1/policies/{id}) for hot reload
    # with no restart, so "embedded" here means co-located, not in-process.
    opa_server_url: str = "http://localhost:8181"
    opa_request_timeout_seconds: float = 2.0

    # --- Execution service: Redis (Celery broker/backend + HITL queue + policy registry) ---
    redis_url: str = "redis://localhost:6379/0"
    policy_registry_key: str = "regengine:policy_registry"
    hitl_key_prefix: str = "regengine:hitl"

    # --- Execution service: Celery ---
    celery_task_default_queue: str = "regengine_default"
    celery_batch_queue: str = "regengine_batch"
    celery_cdc_queue: str = "regengine_cdc"
    celery_webhook_queue: str = "regengine_webhooks"

    # --- Execution service: outbound webhooks (OMS/RMS/broker callbacks) ---
    webhook_hmac_secret: str | None = None
    webhook_timeout_seconds: float = 5.0
    webhook_max_retries: int = 5
    webhook_retry_backoff_seconds: float = 3.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
