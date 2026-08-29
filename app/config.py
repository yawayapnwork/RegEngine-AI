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

    # --- Neo4j Legal Knowledge Graph (app.graph) ---
    # Off by default: a deployment that never enables this needs no Neo4j
    # instance at all, and app.compiler.tasks's sync hook becomes a no-op
    # (see that module's _sync_to_knowledge_graph).
    neo4j_sync_enabled: bool = False
    neo4j_uri: str = "neo4j://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "changeme-dev-only"
    neo4j_database: str = "sebiregulations"

    # --- Compliance rule extraction (CrewAI dual-agent pipeline) ---
    anthropic_api_key: str | None = None
    agent_verbose: bool = False
    agent_max_rpm: int = 20

    # --- Dynamic agent graph orchestration (app.agents.graph) ---
    # Opt-in: False preserves the original fixed two-agent CrewAI
    # sequential pipeline (app.agents.crew.run_dual_validation) exactly as
    # it behaved before dynamic routing existed. True routes
    # app.agents.pipeline.extract_and_audit_clause through the LangGraph
    # state machine instead -- see app.agents.graph's module docstring.
    agent_graph_orchestration_enabled: bool = False
    # Below this extraction_confidence, app.agents.graph's confidence gate
    # routes to the fallback node (a secondary model) instead of
    # proceeding straight to audit -- Requirement 3's "confidence scores
    # below 85%".
    agent_confidence_threshold: float = 0.85
    agent_max_fallback_attempts: int = 2
    # A genuinely different model/checkpoint from the primary
    # (anthropic/claude-3-5-sonnet-20241022, hardcoded in
    # app.agents.crew._build_llm's default) -- see that function's
    # docstring for why a different model, not a retry of the same one.
    agent_fallback_model: str = "anthropic/claude-3-opus-20240229"
    agent_graph_state_key_prefix: str = "regengine:agent_graph"
    agent_graph_state_ttl_seconds: int = 7 * 24 * 3600

    # --- Execution service: embedded OPA engine ---
    # OPA runs as a local/sidecar `opa run --server` process. Policies are
    # pushed via its REST Policy API (PUT /v1/policies/{id}) for hot reload
    # with no restart, so "embedded" here means co-located, not in-process.
    opa_server_url: str = "http://localhost:8181"
    opa_request_timeout_seconds: float = 2.0

    # --- Backtesting service (app.backtest) ---
    # A SEPARATE, isolated OPA instance -- NEVER opa_server_url -- for the
    # optional OpaCandidateEvaluator path. Publishing a not-yet-approved
    # candidate policy bundle here must be impossible to confuse with
    # publishing to production; a distinct setting (rather than a
    # same-URL-plus-namespace convention) makes that mistake require an
    # explicit, deliberate misconfiguration rather than a typo.
    backtest_opa_server_url: str = "http://localhost:8282"
    backtest_concurrency: int = 32  # bounded replay concurrency; see app.backtest.replay_engine
    backtest_key_prefix: str = "regengine:backtest"
    celery_backtest_queue: str = "regengine_backtest"

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
    # --- Regulatory ingestion: RBI / IRDAI / PFRDA source configuration ---
    # (app.ingestion.regulator_sources) -- same shape as the SEBI fields
    # above; kept as separate settings rather than one generic
    # dict-of-lists so each regulator's URLs stay independently
    # overridable via plain env vars (REGENGINE_RBI_RSS_FEED_URLS=... etc).
    rbi_rss_feed_urls: list[str] = Field(
        default_factory=lambda: ["https://www.rbi.org.in/pressreleases_rss.xml"],
        description="RSS/Atom feeds polled for new RBI Master Directions/circulars/notifications",
    )
    rbi_listing_page_urls: list[str] = Field(
        default_factory=lambda: ["https://www.rbi.org.in/Scripts/NotificationUser.aspx"],
        description="HTML listing pages scraped as a fallback/supplement for RBI notifications",
    )
    irdai_rss_feed_urls: list[str] = Field(
        default_factory=list,
        description="RSS/Atom feeds polled for new IRDAI regulations/circulars (IRDAI does not publish a public RSS feed as of writing; populate once/if one becomes available).",
    )
    irdai_listing_page_urls: list[str] = Field(
        default_factory=lambda: ["https://irdai.gov.in/circulars"],
        description="HTML listing pages scraped for IRDAI circulars/regulations",
    )
    pfrda_rss_feed_urls: list[str] = Field(
        default_factory=list,
        description="RSS/Atom feeds polled for new PFRDA circulars (populate once/if PFRDA publishes one).",
    )
    pfrda_listing_page_urls: list[str] = Field(
        default_factory=lambda: ["https://www.pfrda.org.in/index1.cshtml?lsid=204"],
        description="HTML listing pages scraped for PFRDA circulars",
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

    # --- Real-Time Compliance Officer Notifications: Slack & MS Teams ---
    slack_webhook_url: str | None = None
    teams_webhook_url: str | None = None
    slack_signing_secret: str | None = None

    # --- Breach Notification Engine (app.incident) ---
    incident_key_prefix: str = "regengine:incidents"
    incident_events_channel: str = "regengine:incident_events"  # Redis pub/sub -> WebSocket dashboard fanout
    celery_incidents_queue: str = "regengine_incidents"
    # Trigger-matrix defaults (Requirement 1) -- escalation timing in seconds.
    incident_critical_ack_deadline_seconds: int = 15 * 60   # 15 min: PagerDuty/Twilio escalation deadline
    incident_critical_sms_stage_seconds: int = 5 * 60        # 5 min: SMS stage before the 15-min PagerDuty stage
    incident_warning_ack_deadline_seconds: int = 30 * 60     # 30 min: email escalation for unacknowledged warnings
    incident_escalation_sweep_interval_seconds: int = 60     # safety-net sweep cadence (see app.incident.tasks)

    # PagerDuty Events API v2 (https://developer.pagerduty.com/api-reference/)
    pagerduty_routing_key: str | None = None
    pagerduty_api_base_url: str = "https://events.pagerduty.com/v2"

    # Twilio REST API (SMS)
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    twilio_oncall_phone_numbers: list[str] = Field(default_factory=list)
    twilio_api_base_url: str = "https://api.twilio.com/2010-04-01"

    # Email (SMTP) escalation channel
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_from_address: str = "regengine-alerts@example.com"
    compliance_officer_email_list: list[str] = Field(default_factory=list)

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
    # Kept for backward compatibility with a single-IdP deployment; a
    # multi-IdP deployment configures the named Okta/Azure AD/PingIdentity
    # blocks below instead (app.security.sso_providers merges both into
    # one provider registry, keyed by issuer).
    jwt_external_issuer: str | None = None
    jwt_jwks_url: str | None = None
    jwt_external_algorithms: list[str] = Field(default_factory=lambda: ["RS256"])
    tenant_client_key_prefix: str = "regengine:tenant_clients"

    # --- Enterprise SSO: named identity providers (app.security.sso_providers) ---
    # Each block is independently optional -- configure only the IdP(s)
    # this deployment's institutional intermediaries actually use. All
    # three are OIDC-compatible; `group_claim` is the ID token claim each
    # IdP puts AD/directory group membership in (Okta and Azure AD both
    # default to "groups" when group claims are enabled in the app
    # registration/authorization server, but this is renamed often enough
    # in practice that it stays configurable per IdP rather than assumed).
    sso_okta_issuer: str | None = None  # e.g. "https://your-org.okta.com/oauth2/default"
    sso_okta_jwks_url: str | None = None  # e.g. "https://your-org.okta.com/oauth2/default/v1/keys"
    sso_okta_audience: str | None = None
    sso_okta_group_claim: str = "groups"

    sso_azure_ad_issuer: str | None = None  # e.g. "https://login.microsoftonline.com/{tenant_id}/v2.0"
    sso_azure_ad_jwks_url: str | None = None  # e.g. "https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    sso_azure_ad_audience: str | None = None  # the app registration's Application (client) ID
    sso_azure_ad_group_claim: str = "groups"

    sso_pingidentity_issuer: str | None = None
    sso_pingidentity_jwks_url: str | None = None
    sso_pingidentity_audience: str | None = None
    sso_pingidentity_group_claim: str = "groups"

    sso_external_algorithms: list[str] = Field(default_factory=lambda: ["RS256"])

    # --- Automated Directory Sync: AD/Okta/Azure AD group -> RBAC role ---
    # Evaluated on every token decode (claims-based, zero-latency, the
    # standard OIDC pattern) -- see app.security.directory_sync. The
    # supplementary periodic API-polling sync
    # (app.security.directory_sync_job) additionally catches a group
    # membership CHANGE taking effect before a long-lived token's natural
    # expiry, by writing proactive per-subject role overrides here.
    sso_directory_group_role_map: dict[str, str] = Field(
        default_factory=lambda: {
            "SEBI_Compliance_Team": "Compliance_Officer",
            "IT_Audit_Group": "System_Admin",
        },
        description="IdP group name -> app.security.models.Role value.",
    )
    directory_sync_override_key_prefix: str = "regengine:directory_sync_override"
    directory_sync_poll_interval_seconds: int = 900  # 15 min
    celery_security_queue: str = "regengine_security"

    # Okta/Azure AD directory APIs, used only by the supplementary polling
    # sync job (scripts/sso and app.security.directory_sync_job), never by
    # the request-time auth path.
    okta_org_url: str | None = None  # e.g. "https://your-org.okta.com"
    okta_api_token: str | None = None
    azure_ad_tenant_id: str | None = None
    azure_ad_client_id: str | None = None
    azure_ad_client_secret: str | None = None

    # --- Session Management & Step-Up MFA ---
    session_key_prefix: str = "regengine:sessions"
    # Idle timeout: no activity for this long ends the session even though
    # the underlying JWT hasn't expired yet -- the actual "strict session
    # timeout" enterprise SSO deployments expect, independent of whatever
    # (possibly long) lifetime the IdP issued the token with.
    session_idle_timeout_seconds: int = 15 * 60
    # Absolute timeout: a session is force-ended this long after login
    # regardless of activity, bounding how long a stolen/left-open session
    # can ever be used for.
    session_absolute_timeout_seconds: int = 8 * 3600
    # Step-up MFA freshness window: an authentication event (`auth_time`/
    # `amr` claim, or a session's own recorded MFA timestamp) older than
    # this is no longer considered "fresh enough" to authorize a
    # high-privilege operation (e.g. approving a compiled OPA policy) --
    # the caller must re-authenticate with the IdP (a fresh MFA prompt)
    # first. See app.security.step_up.
    step_up_mfa_max_age_seconds: int = 5 * 60
    # OIDC AMR (Authentication Methods Reference, RFC 8176) values treated
    # as satisfying MFA -- "pwd" alone (password only) never counts.
    step_up_required_amr_values: list[str] = Field(
        default_factory=lambda: ["mfa", "otp", "hwk", "swk", "sms", "face", "fpt"]
    )
    step_up_redirect_base_url: str | None = None  # IdP re-auth URL template surfaced to the client on a 401 step-up challenge

    # --- SAML 2.0 (app.api.saml_routes, via python3-saml) ---
    saml_enabled: bool = False
    saml_sp_entity_id: str = "https://regengine.internal/saml/metadata"
    saml_sp_acs_url: str = "https://regengine.internal/v1/auth/saml/acs"
    saml_idp_entity_id: str | None = None
    saml_idp_sso_url: str | None = None
    saml_idp_x509_cert: str | None = None  # IdP's public signing certificate (PEM, no header/footer needed by python3-saml)
    saml_sp_x509_cert: str | None = None  # only required if this SP signs its own AuthnRequests
    saml_sp_private_key: str | None = None
    saml_group_attribute_name: str = "http://schemas.xmlsoap.org/claims/Group"  # ADFS/AD FS default; override per IdP

    # --- Audit binder digital signature (app.reporting.signing) ---
    # RSA-PSS/SHA-256 over the audit binder's manifest -- a real
    # asymmetric signature (not an HMAC) deliberately, so a SEBI auditor
    # can verify authenticity with only the PUBLIC key, years later, with
    # no shared secret ever having left this service. Distinct key pair
    # from jwt_private_key_pem/jwt_public_key_pem: a token-signing key and
    # a document-signing key are different trust boundaries with
    # different rotation schedules, and must never be the same key.
    audit_binder_signing_private_key_pem: str | None = None
    audit_binder_signing_public_key_pem: str | None = None
    audit_binder_signer_id: str = "RegEngine AI Compliance Reporting Service"

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

    # --- Multi-tenant partitioning ---
    # Namespaced Redis key prefix for per-tenant policy registries.
    # Each tenant's registry lives at <prefix>:<tenant_id>, e.g.
    # "regengine:policy_registry:stockbroker_a".  The flat (non-tenant)
    # policy_registry_key above is retained for backwards compatibility
    # with the non-tenant-aware PolicyRegistry used in the baseline path.
    tenant_policy_registry_key_prefix: str = "regengine:policy_registry"

    # PostgreSQL GUC used by RLS policies (must match sql/rls_tenant_partitioning.sql).
    # Documented here so ops staff can grep a single source of truth.
    db_tenant_guc: str = "app.current_tenant_id"
    db_admin_sentinel: str = "__admin__"

    # --- Analytics & reporting ---
    # Maximum ledger rows returned in a single audit-trail JSON page.
    analytics_audit_trail_max_page_size: int = Field(
        default=2000,
        description="Hard cap on audit-trail page_size query param.",
    )
    # Anomaly detection z-score thresholds (can be tuned without a code deploy).
    analytics_anomaly_z_high: float = Field(
        default=3.0,
        description="Z-score threshold for HIGH-severity anomaly classification.",
    )
    analytics_anomaly_z_low: float = Field(
        default=2.0,
        description="Z-score threshold for LOW-severity anomaly classification.",
    )
    # PDF generation: output directory for locally cached report files.
    # Set to None to disable on-disk caching (PDFs are streamed directly).
    analytics_pdf_cache_dir: str | None = Field(
        default=None,
        description="Optional filesystem path for caching generated PDF files.",
    )
    # Maximum date-range span (in days) a single analytics query may cover.
    # Prevents accidental full-table scans on a multi-year ledger.
    analytics_max_range_days: int = Field(
        default=366,
        description="Max calendar days a single analytics/reporting query window may span.",
    )

    # --- Sandbox rule-testing environment ---
    # Maximum number of transactions the sandbox dry-run API will evaluate
    # in a single request (prevents accidental DoS via large batches).
    sandbox_max_transactions: int = Field(
        default=50,
        description="Max transactions per sandbox dry-run request.",
    )
    # Maximum number of historical circulars the sandbox will return in a
    # single listing response (for the circular-browse endpoint).
    sandbox_max_circulars: int = Field(
        default=100,
        description="Max circulars returned by the sandbox circular listing endpoint.",
    )
    # Sandbox OPA evaluation timeout -- slightly longer than the production
    # timeout (opa_request_timeout_seconds) to accommodate un-optimised
    # test rules that haven't been tuned for latency yet.
    sandbox_opa_timeout_seconds: float = Field(
        default=5.0,
        description="Per-policy OPA timeout for sandbox dry-run evaluations.",
    )
    # Sandbox sessions are strictly read-only (always rolled back); this flag
    # lets operators disable the sandbox entirely without a code deploy if a
    # compliance concern arises (e.g. an audit period where no speculative
    # rule changes should be attempted).
    sandbox_enabled: bool = Field(
        default=True,
        description="Set false to disable the sandbox API entirely (returns 503).",
    )

    # --- LLM cost optimization: semantic cache + model-tier routing ---
    # (app.llm_ops) -- separate Qdrant collection from qdrant_collection
    # (the production clause index) so cache entries never leak into or
    # get pruned alongside indexed circular content.
    llm_cache_qdrant_collection: str = "llm_semantic_cache"
    llm_cache_similarity_threshold: float = Field(
        default=0.97,
        description="Cosine similarity above which a cached response is reused instead of re-invoking the LLM. High by design -- a near-miss on legal text can flip an obligation's meaning.",
    )
    llm_cache_ttl_seconds: int = Field(
        default=30 * 24 * 3600,
        description="Cache entry lifetime. Bounded (not permanent) so a later SEBI circular amendment/rescission eventually invalidates stale cached extractions even if the clause text is byte-identical to a superseded one.",
    )
    llm_cache_redis_key_prefix: str = "regengine:llm_cache"

    # Local low-cost tier: the QLoRA-fine-tuned model served by
    # llm_finetune/vllm (or Ollama) for deterministic/simple clauses.
    llm_router_cheap_model: str = "sebi-compliance-llm"
    llm_router_cheap_model_base_url: str = "http://localhost:8000/v1"
    llm_router_frontier_model: str = "anthropic/claude-3-5-sonnet-20241022"
    # Below this confidence (or on a schema-validation failure) from the
    # cheap tier, the router escalates to the frontier model rather than
    # accepting a low-confidence local-model extraction.
    llm_router_escalation_confidence_threshold: float = 0.75
    celery_llm_ops_queue: str = "regengine_llm_ops"
    llm_cache_purge_interval_seconds: int = 3600  # hourly sweep of expired semantic-cache Qdrant points

    # --- Zero-knowledge proof verification (app.zkp, app.api.zkp_routes) ---
    # Lets a broker prove `collected_margin >= required_margin` (see
    # zk/circuits/margin_compliance.circom) without ever sending the
    # margin amount or client account identifier to this server. Off by
    # default like every other optional subsystem this session added --
    # a deployment that never onboards a zk-capable broker needs no
    # verification keys configured and py_ecc's pairing arithmetic never
    # runs.
    zkp_enabled: bool = False
    # circuit_id -> filesystem path to that circuit's snarkjs-exported
    # verification_key.json (see zk/scripts/build_circuit.sh's step 4).
    # A dict (not a single path) because a deployment may run more than
    # one zk-provable rule concurrently as new circuits are added.
    zkp_verification_keys: dict[str, str] = Field(
        default_factory=lambda: {"margin_compliance_v1": "zk/build/verification_key.json"}
    )

    # --- Self-healing policy repair loop (app.healing) ---
    # Intercepts an OPA compile/publish failure or a JSON-Logic runtime
    # crash and attempts automated repair before falling back to the
    # existing DLQ/HITL escalation paths -- see app/healing/orchestrator.py's
    # module docstring for how this composes with (not replaces)
    # app.resilience.exceptions.MalformedASTError's existing
    # non-retryable-by-design DLQ routing. Off by default: a deployment
    # that hasn't reviewed the repair agent's prompt/behavior should keep
    # getting today's "straight to DLQ" behavior unchanged.
    policy_self_healing_enabled: bool = False
    policy_self_healing_max_retries: int = 3
    policy_self_healing_key_prefix: str = "regengine:policy_healing"

    # --- Regulatory filing adapter (app.regulatory_filing) ---
    # Packages compliance-log/daily-collateral evidence into SEBI/MII
    # e-filing schemas, signs it (X.509/PKCS#7), and submits it via SFTP
    # or a regulatory portal API. Off by default like every other
    # optional subsystem here -- a deployment that hasn't onboarded a
    # specific MII's filing schedule shouldn't have a Celery beat entry
    # silently trying to submit anything.
    regulatory_filing_enabled: bool = False
    regulatory_filing_reporting_entity_code: str = "INZ000000000"  # SEBI broker registration number; placeholder default
    regulatory_filing_key_prefix: str = "regengine:regulatory_filing"
    regulatory_filing_max_retries: int = 5
    regulatory_filing_submit_interval_seconds: int = 300
    celery_regulatory_filing_queue: str = "regengine_regulatory_filing"

    # PKI signing backend: "software" (private key + X.509 cert held in
    # this process/secrets backend -- see app.regulatory_filing.signing)
    # or "hsm" (PKCS#11 token via an OpenSSL engine -- see that module's
    # HSMSigningBackend docstring for why HSM signing shells out to the
    # openssl CLI rather than calling a PKCS#11 library directly for the
    # PKCS#7/CMS envelope itself).
    regulatory_filing_signing_backend: str = "software"
    regulatory_filing_signing_cert_pem: str | None = None
    regulatory_filing_signing_private_key_pem: str | None = None
    regulatory_filing_hsm_pkcs11_module_path: str | None = None
    regulatory_filing_hsm_key_uri: str | None = Field(
        None, description='RFC 7512 PKCS#11 URI identifying the HSM-resident signing key, e.g. "pkcs11:token=RegEngineHSM;object=sebi-filing-key;type=private".'
    )
    regulatory_filing_hsm_engine_id: str = "pkcs11"  # OpenSSL engine id -- "pkcs11" (the standard OpenSC engine) unless the HSM vendor ships its own

    # SFTP submission target (NSDL/CDSL/NSE/BSE each publish their own
    # SFTP host/path conventions per MII -- these are per-deployment).
    regulatory_filing_sftp_host: str | None = None
    regulatory_filing_sftp_port: int = 22
    regulatory_filing_sftp_username: str | None = None
    regulatory_filing_sftp_private_key_pem: str | None = None
    regulatory_filing_sftp_password: str | None = None  # only if the MII's SFTP endpoint doesn't support key auth
    regulatory_filing_sftp_remote_dir: str = "/incoming"
    regulatory_filing_sftp_known_host_key: str | None = Field(
        None, description="The MII SFTP host's public key (OpenSSH 'known_hosts' line format) -- required in production; connecting with host-key checking disabled is a submission-integrity risk for a regulatory filing."
    )

    # SEBI regulatory portal API (REST) submission target, as an
    # alternative to SFTP for MIIs/regulators that publish an API instead.
    regulatory_filing_portal_api_base_url: str | None = None
    regulatory_filing_portal_api_key: str | None = None

    # --- Compliance Chaos Monkey (chaos.monkey) ---
    # A safety rail, not a feature toggle: chaos.monkey.runner.ChaosMonkeyRunner
    # refuses to inject faults against any real engine/service unless this is
    # explicitly True, so a staging-only environment file has to opt in on
    # purpose rather than a chaos run ever being one accidental invocation
    # away from touching production. False everywhere by default, same as
    # every other optional subsystem in this file.
    chaos_monkey_enabled: bool = False
    chaos_monkey_postmortem_dir: str = "chaos/postmortems"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
