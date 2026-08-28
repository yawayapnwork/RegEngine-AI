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
    # L1 in-process PolicyCache TTL safety net (app/execution/policy_cache.py) --
    # short on purpose; event-driven invalidation via pub/sub
    # (app/execution/policy_hot_reload.py) is the primary mechanism, this
    # only bounds the staleness window if a pub/sub message is ever dropped.
    policy_cache_ttl_seconds: float = 30.0

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

    # --- Main application schema (circulars, clauses, compiled_rules, hitl_reviews) ---
    # Same Postgres database as the ledger by default, but a distinct,
    # ordinary-privilege role -- see app/db/session.py for why this is kept
    # separate from ledger_database_url.
    database_url: str = "postgresql+asyncpg://regengine_app:changeme@localhost:5432/regengine"
    database_pool_size: int = 10

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

    # --- Security: JWT / OAuth2 ---
    # Self-issued tokens (Broker_API_Client, via POST /v1/auth/token).
    jwt_algorithm: str = "HS256"  # or "RS256"; see app/security/jwt.py
    jwt_secret_key: str = "changeme-dev-only-use-secrets-backend-in-prod"  # HS256; resolved via app.security.secrets in prod
    jwt_public_key_pem: str | None = None  # RS256 verification key
    jwt_private_key_pem: str | None = None  # RS256 signing key
    jwt_issuer: str = "regengine-ai"
    jwt_audience: str = "regengine-ai-api"
    jwt_access_token_ttl_seconds: int = 3600
    # External SSO (Compliance_Officer / System_Admin), verified via JWKS.
    jwt_external_issuer: str | None = None
    jwt_jwks_url: str | None = None
    jwt_external_algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    tenant_client_key_prefix: str = "regengine:tenant_clients"

    # --- Security: secrets management backend ---
    secrets_backend: str = "env"  # "env" | "aws" | "vault" -- see app/security/secrets.py
    secrets_cache_ttl_seconds: float = 300.0
    aws_secrets_region: str = "ap-south-1"
    vault_addr: str = "http://localhost:8200"
    vault_token: str | None = None
    vault_kv_mount: str = "secret"

    # --- Security: transport / rate limiting / payload encryption ---
    # Defaults to False so the service still boots cleanly over plain HTTP
    # in local dev / docker-compose (neither terminates TLS itself -- see
    # docker-compose.yml). Set true in production, where TLS is terminated
    # at the ingress/load balancer and X-Forwarded-Proto is trustworthy --
    # helm/regengine-ai's values.yaml.config sets this explicitly.
    enforce_https: bool = False
    rate_limit_key_prefix: str = "regengine:ratelimit"
    rate_limit_window_seconds: int = 60
    rate_limit_requests_per_window: int = 300
    rate_limit_exempt_paths: list[str] = Field(
        default_factory=lambda: ["/healthz", "/", "/docs", "/openapi.json", "/redoc", "/metrics"]
    )
    payload_encryption_enabled: bool = False

    # --- Observability: OpenTelemetry tracing ---
    otel_enabled: bool = True
    otel_service_name: str = "regengine-ai"
    # None -> ConsoleSpanExporter (stdout), safe zero-config local-dev
    # default. Point at a real collector (e.g. "http://localhost:4318/v1/traces"
    # for the OTel Collector's OTLP/HTTP receiver) in every other environment.
    otel_exporter_otlp_endpoint: str | None = None
    otel_traces_sample_ratio: float = 1.0  # 1.0 = trace every request; lower in high-volume prod

    # --- Observability: Prometheus metrics ---
    metrics_enabled: bool = True
    # hitl_review_queue_depth (a Gauge) is updated by a periodic background
    # poll, not per-event -- see app/observability/metrics.py's module
    # docstring for why a gauge like this is pull-refreshed rather than
    # incrementally maintained.
    metrics_queue_depth_poll_interval_seconds: float = 15.0

    # --- Resiliency: Dead-Letter Queue + retry policy (app/resilience/) ---
    dlq_key_prefix: str = "regengine:dlq"
    celery_agents_queue: str = "regengine_agents"  # LLM extraction/audit -- isolated so a slow LLM call never blocks ingestion/compilation
    celery_compiler_queue: str = "regengine_compiler"
    celery_vectorstore_queue: str = "regengine_vectorstore"
    # Applied via retry_backoff/retry_backoff_max/retry_jitter task options
    # (native Celery, full-jitter algorithm) on every retryable task -- see
    # app/resilience/retry_policy.py's module docstring.
    retry_backoff_base_seconds: int = 2
    retry_backoff_max_seconds: int = 300
    retry_max_attempts_network: int = 5  # RSS polling, vector DB ingestion -- transient, worth persisting through
    retry_max_attempts_pipeline: int = 3  # PDF parsing, LLM extraction -- a couple of attempts, then a human looks


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
