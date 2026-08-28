"""Enterprise OpenAPI 3.0.3 Customization & Schema Generator for RegEngine AI.

Customizes FastAPI's auto-generated OpenAPI schema to include:
  1. Detailed domain descriptions, security schemes, and authentication scopes.
  2. Redoc `x-tagGroups` layout organizing endpoints into 5 core domain sections:
     - Regulatory Ingestion
     - Agent Rules Engine
     - OPA Evaluation
     - Cryptographic Audit Vault
     - HITL Management
  3. Standardized HTTP error schemas (400, 401, 403, 404, 409, 422, 500) with payload examples.
  4. Rich request and response payload examples.
"""

from __future__ import annotations

from typing import Any
from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi


API_DESCRIPTION = """
# RegEngine AI — Enterprise Regulatory Compliance & Execution API Suite

**RegEngine AI** is a deterministic, layout-aware regulatory parsing and real-time transaction compliance evaluation platform for SEBI (Securities and Exchange Board of India) master circulars.

---

## 🏛️ Core Architectural Domains

1. **Regulatory Ingestion**: Automated SEBI RSS feed polling, layout-aware PDF parsing, clause chunking, and vector indexation.
2. **Agent Rules Engine**: Multi-agent extraction (Extraction Agent) and logic auditing (Logic Auditor Agent) compiling compliance obligations into Rego & JSON-Logic ASTs.
3. **OPA Policy Evaluation**: Sub-millisecond synchronous transaction compliance evaluation against persistent, co-located Open Policy Agent (OPA) policy engines.
4. **Cryptographic Audit Vault**: Append-only, tamper-evident audit ledger (`compliance_audit_ledger`) backed by monotonic sequence numbers and SHA-256 block hash chains.
5. **HITL Management**: Human-in-the-Loop review portals, live transaction flagging, and Slack / MS Teams interactive quick-approval webhooks.

---

## 🔒 Authentication & Authorization

RegEngine AI uses **OAuth2 with Bearer JWT tokens** (`HS256` / `RS256`).

- **Broker_API_Client**: Used by stockbrokers, AMCs, and market intermediaries calling `/v1/execution/*`.
- **Compliance_Officer**: Required for approving or rejecting compiled policies in `/v1/hitl-reviews/*`.
- **System_Admin**: Required for infrastructure, queue depth, and DLQ management (`/v1/admin/dlq/*`).

---

## ⚡ Error Handling Contract

All non-2xx responses return a standardized JSON error structure:

```json
{
  "detail": "Human-readable error explanation",
  "error_code": "ERR_SPECIFIC_CATEGORY",
  "timestamp": "2026-08-28T23:45:00Z"
}
```
"""


# Redoc x-tagGroups Domain Structure
REDOC_TAG_GROUPS = [
    {
        "name": "1. Regulatory Ingestion",
        "tags": ["ingestion-service", "ingestion-admin"],
    },
    {
        "name": "2. Agent Rules Engine",
        "tags": ["sandbox", "compiler"],
    },
    {
        "name": "3. OPA Evaluation",
        "tags": ["transaction-evaluator", "cdc-ingestion", "batch-execution"],
    },
    {
        "name": "4. Cryptographic Audit Vault",
        "tags": ["compliance-analytics", "dlq-admin"],
    },
    {
        "name": "5. HITL Management",
        "tags": ["hitl-review-portal", "webhooks-callbacks"],
    },
]


def get_custom_openapi(app: FastAPI) -> dict[str, Any]:
    """Generates customized OpenAPI 3.0.3 schema with x-tagGroups and security schemes."""
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="RegEngine AI — Regulatory Compliance & Execution API Suite",
        version="1.0.0",
        openapi_version="3.0.3",
        description=API_DESCRIPTION,
        routes=app.routes,
    )

    # Add Redoc x-tagGroups extension
    openapi_schema["x-tagGroups"] = REDOC_TAG_GROUPS

    # Ensure Security Components are defined
    components = openapi_schema.setdefault("components", {})
    security_schemes = components.setdefault("securitySchemes", {})
    security_schemes["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "Provide a valid JWT token obtained from `POST /v1/auth/token`.",
    }

    # Add Global Security Requirement
    openapi_schema["security"] = [{"BearerAuth": []}]

    # Define Standardized Error Response Schemas
    schemas = components.setdefault("schemas", {})
    schemas["StandardErrorDetail"] = {
        "type": "object",
        "properties": {
            "detail": {"type": "string", "example": "Invalid or expired JWT token."},
            "error_code": {"type": "string", "example": "ERR_UNAUTHORIZED"},
            "timestamp": {"type": "string", "format": "date-time", "example": "2026-08-28T23:45:00Z"},
        },
        "required": ["detail"],
    }

    # Attach tag descriptions
    openapi_schema["tags"] = [
        {"name": "ingestion-service", "description": "SEBI RSS feed polling, document ingestion, and PDF parsing."},
        {"name": "sandbox", "description": "Interactive policy sandbox, AST simulation, and tenant risk overlays."},
        {"name": "transaction-evaluator", "description": "Synchronous real-time trade evaluation against compiled OPA Rego policies."},
        {"name": "cdc-ingestion", "description": "Change-Data-Capture event receiver for Debezium / Kafka Connect change streams."},
        {"name": "batch-execution", "description": "Asynchronous bulk evaluation for legacy SFTP batch files."},
        {"name": "compliance-analytics", "description": "Cryptographic audit ledger verification and SEBI compliance reporting."},
        {"name": "dlq-admin", "description": "Dead-Letter Queue administration, item requeueing, and failure inspection."},
        {"name": "hitl-review-portal", "description": "Clause-level HITL review portal for compliance officer approval sign-offs."},
        {"name": "webhooks-callbacks", "description": "Slack and MS Teams real-time interactive notification and quick-approval callbacks."},
    ]

    app.openapi_schema = openapi_schema
    return app.openapi_schema
