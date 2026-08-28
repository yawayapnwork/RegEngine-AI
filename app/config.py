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
    celery_ingestion_queue: str = "regengine_ingestion"

    # --- Execution service: outbound webhooks (OMS/RMS/broker callbacks) ---
    webhook_hmac_secret: str | None = None
    webhook_timeout_seconds: float = 5.0
    webhook_max_retries: int = 5
    webhook_retry_backoff_seconds: float = 3.0

    # --- Tamper-evident audit ledger (PostgreSQL, SHA-256 hash chain) ---
    ledger_database_url: str = "postgresql+asyncpg://regengine_ledger_writer:changeme@localhost:5432/regengine"
    ledger_pool_size: int = 10

    # --- Regulatory ingestion: SEBI circular monitoring ---
    sebi_rss_feed_urls: list[str] = Field(
        default_factory=lambda: [
            "https://www.sebi.gov.in/sebirss.xml",
            "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doRss=yes&sectype=Circulars",
        ],
        description="RSS/Atom feeds polled for new SEBI circulars/notifications",
    )
    sebi_listing_page_urls: list[str] = Field(
        default_factory=lambda: [
            "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=6&smid=0",  # Circulars
            "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=1&ssid=7&smid=0",  # Master Circulars
        ],
        description="HTML listing pages scraped as a fallback/supplement when the RSS feed lags or omits an item",
    )
    ingestion_poll_interval_seconds: int = 900  # 15 min; SEBI publishes intermittently, not real-time
    ingestion_request_min_interval_seconds: float = 2.0  # min gap between requests to sebi.gov.in
    ingestion_request_timeout_seconds: float = 20.0
    ingestion_max_retries: int = 4
    ingestion_retry_backoff_base_seconds: float = 2.0
    ingestion_retry_backoff_max_seconds: float = 60.0
    ingestion_user_agents: list[str] = Field(
        default_factory=lambda: [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4 Safari/605.1.15",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36",
        ],
        description="Rotated User-Agent pool; identifies this service as a standard browser client",
    )
    ingestion_contact_email: str = "yashikayapsandworks@gmail.com"
    ingestion_proxy_urls: list[str] = Field(
        default_factory=list,
        description="Optional outbound proxy pool (e.g. ['http://user:pass@proxy1:8080', ...]); "
        "empty means requests go direct. Rotated round-robin per request.",
    )
    ingestion_respect_robots_txt: bool = True
    ingestion_state_key_prefix: str = "regengine:ingestion"
    ingestion_pdf_download_dir: str = "./data/ingested_pdfs"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
