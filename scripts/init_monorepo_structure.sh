#!/usr/bin/env bash
# RegEngine AI monorepo initializer.
#
# Scaffolds the microservices-split layout below, alongside (not instead
# of) whatever already exists in the repo -- this script is idempotent
# and NON-DESTRUCTIVE: every file it writes is skipped, not overwritten,
# if a file at that path already exists. Run it as many times as you
# like; it only ever fills in what's missing.
#
#   /services/ingestion   FastAPI + Tika + Qdrant
#   /services/agents      CrewAI + Qwen2.5 (Hugging Face Inference)
#   /services/compiler    OPA Rego generator
#   /services/execution   FastAPI + Redis + OPA Wasm
#   /services/audit       PostgreSQL SHA-256 vault
#   /frontend             React + Tailwind IDE
#   /deploy               Helm charts + Kubernetes manifests
#
# Usage:
#   chmod +x scripts/init_monorepo_structure.sh
#   ./scripts/init_monorepo_structure.sh
#
# Run from the repository root (the script also works from anywhere --
# it resolves paths relative to its own location, two directories up).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CREATED=0
SKIPPED=0

# Writes stdin to $1, creating parent directories as needed. Skips (does
# NOT overwrite) if the file already exists -- the core safety property
# this whole script relies on, since the repo root already has a real,
# populated requirements.txt/.env.example this script must never clobber.
create_file() {
  local path="$1"
  if [ -f "$path" ]; then
    echo "  skip (exists)   $path"
    cat >/dev/null
    SKIPPED=$((SKIPPED + 1))
  else
    mkdir -p "$(dirname "$path")"
    cat >"$path"
    echo "  created         $path"
    CREATED=$((CREATED + 1))
  fi
}

mkdir -p services/ingestion services/agents services/compiler services/execution services/audit
mkdir -p frontend deploy/helm/regengine/templates deploy/k8s

# ---------------------------------------------------------------------
# services/ingestion -- FastAPI + Tika + Qdrant
# ---------------------------------------------------------------------
echo "== services/ingestion =="

create_file services/ingestion/app/__init__.py <<'EOF'
EOF

create_file services/ingestion/app/main.py <<'EOF'
"""Ingestion service: pulls SEBI circulars, extracts layout-aware text
via Apache Tika, chunks it into clauses, and indexes the resulting
embeddings into Qdrant. This file is a scaffold -- wire it up to the
real extraction/chunking/indexing pipeline before deploying."""
from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI(title="RegEngine AI - Ingestion Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "ingestion"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    """Extend this to actually ping Tika and Qdrant before reporting ready."""
    return {
        "status": "ok",
        "tika_server_url": os.environ.get("TIKA_SERVER_URL", "not set"),
        "qdrant_url": os.environ.get("QDRANT_URL", "not set"),
    }
EOF

create_file services/ingestion/requirements.txt <<'EOF'
fastapi>=0.111
uvicorn[standard]>=0.30
pydantic-settings>=2.3
unstructured[pdf]>=0.15
tika>=2.6.0
sentence-transformers>=3.0
qdrant-client>=1.10
feedparser>=6.0
beautifulsoup4>=4.12
lxml>=5.2
EOF

create_file services/ingestion/pyproject.toml <<'EOF'
[project]
name = "regengine-ingestion"
version = "0.1.0"
description = "RegEngine AI ingestion service: Tika extraction + Qdrant indexing."
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
EOF

create_file services/ingestion/Dockerfile <<'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
EOF

create_file services/ingestion/README.md <<'EOF'
# Ingestion Service

FastAPI service responsible for pulling SEBI circulars, extracting
layout-aware text via Apache Tika, chunking clauses, and indexing
embeddings into Qdrant.

Run locally: `uvicorn app.main:app --reload --port 8001`
EOF

# ---------------------------------------------------------------------
# services/agents -- CrewAI + Qwen2.5 (Hugging Face Inference)
# ---------------------------------------------------------------------
echo "== services/agents =="

create_file services/agents/app/__init__.py <<'EOF'
EOF

create_file services/agents/app/main.py <<'EOF'
"""Agents service: dual-agent (Extraction + Logic Auditor) compliance
rule extraction via CrewAI, backed by Qwen2.5 (via Hugging Face
Inference). This file is a
scaffold -- wire it up to the real CrewAI crew/task definitions before
deploying."""
from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI(title="RegEngine AI - Agents Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "agents"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {
        "status": "ok",
        "hf_api_token_configured": str(bool(os.environ.get("HUGGINGFACEHUB_API_TOKEN") or os.environ.get("HF_TOKEN"))),
    }
EOF

create_file services/agents/requirements.txt <<'EOF'
fastapi>=0.111
uvicorn[standard]>=0.30
pydantic-settings>=2.3
crewai>=0.70
crewai-tools>=0.12
litellm>=1.44
EOF

create_file services/agents/pyproject.toml <<'EOF'
[project]
name = "regengine-agents"
version = "0.1.0"
description = "RegEngine AI agents service: CrewAI dual-agent extraction on Qwen2.5 (via Hugging Face Inference)."
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
EOF

create_file services/agents/Dockerfile <<'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]
EOF

create_file services/agents/README.md <<'EOF'
# Agents Service

Dual-agent (Extraction + Logic Auditor) compliance rule extraction via
CrewAI, backed by Qwen2.5 (via Hugging Face Inference).

Run locally: `uvicorn app.main:app --reload --port 8002`
EOF

# ---------------------------------------------------------------------
# services/compiler -- OPA Rego generator
# ---------------------------------------------------------------------
echo "== services/compiler =="

create_file services/compiler/app/__init__.py <<'EOF'
EOF

create_file services/compiler/app/main.py <<'EOF'
"""Compiler service: turns an audited, extracted compliance rule into
an OPA Rego policy module (and/or a JSON-Logic fallback AST). This file
is a scaffold -- wire it up to the real Rego/JSON-Logic compiler before
deploying."""
from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="RegEngine AI - Compiler Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "compiler"}
EOF

create_file services/compiler/requirements.txt <<'EOF'
fastapi>=0.111
uvicorn[standard]>=0.30
pydantic-settings>=2.3
jsonschema>=4.22
EOF

create_file services/compiler/pyproject.toml <<'EOF'
[project]
name = "regengine-compiler"
version = "0.1.0"
description = "RegEngine AI compiler service: OPA Rego / JSON-Logic policy generation."
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
EOF

create_file services/compiler/Dockerfile <<'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8003
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
EOF

create_file services/compiler/README.md <<'EOF'
# Compiler Service

Compiles audited, extracted compliance rules into OPA Rego policy
modules (and a JSON-Logic fallback AST for non-OPA consumers).

Run locally: `uvicorn app.main:app --reload --port 8003`
EOF

# ---------------------------------------------------------------------
# services/execution -- FastAPI + Redis + OPA Wasm
# ---------------------------------------------------------------------
echo "== services/execution =="

create_file services/execution/app/__init__.py <<'EOF'
EOF

create_file services/execution/app/main.py <<'EOF'
"""Execution service: evaluates live broker transactions against
compiled OPA policy (server or Wasm-embedded), backed by Redis for the
policy registry and HITL queue. This file is a scaffold -- wire it up
to the real evaluator before deploying."""
from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI(title="RegEngine AI - Execution Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "execution"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {
        "status": "ok",
        "redis_url": os.environ.get("REDIS_URL", "not set"),
        "opa_server_url": os.environ.get("OPA_SERVER_URL", "not set"),
    }
EOF

create_file services/execution/requirements.txt <<'EOF'
fastapi>=0.111
uvicorn[standard]>=0.30
pydantic-settings>=2.3
httpx>=0.27
redis>=5.0
celery[redis]>=5.4
EOF

create_file services/execution/pyproject.toml <<'EOF'
[project]
name = "regengine-execution"
version = "0.1.0"
description = "RegEngine AI execution service: live transaction evaluation against compiled OPA policy."
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
EOF

create_file services/execution/Dockerfile <<'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8004
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"]
EOF

create_file services/execution/README.md <<'EOF'
# Execution Service

Evaluates live broker transactions against compiled OPA policy
(co-located server, or a Wasm-compiled bundle embedded in-process for
the sub-millisecond hot path), backed by Redis for the policy registry
and HITL queue.

Run locally: `uvicorn app.main:app --reload --port 8004`
EOF

# ---------------------------------------------------------------------
# services/audit -- PostgreSQL SHA-256 vault
# ---------------------------------------------------------------------
echo "== services/audit =="

create_file services/audit/app/__init__.py <<'EOF'
EOF

create_file services/audit/app/main.py <<'EOF'
"""Audit service: append-only, SHA-256 hash-chained compliance ledger
on PostgreSQL. This file is a scaffold -- wire it up to the real ledger
service (append/verify) before deploying."""
from __future__ import annotations

import os

from fastapi import FastAPI

app = FastAPI(title="RegEngine AI - Audit Service", version="0.1.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "audit"}


@app.get("/readyz")
async def readyz() -> dict[str, str]:
    return {
        "status": "ok",
        "ledger_database_configured": str(bool(os.environ.get("LEDGER_DATABASE_URL"))),
    }
EOF

create_file services/audit/requirements.txt <<'EOF'
fastapi>=0.111
uvicorn[standard]>=0.30
pydantic-settings>=2.3
sqlalchemy[asyncio]>=2.0
asyncpg>=0.29
alembic>=1.13
EOF

create_file services/audit/pyproject.toml <<'EOF'
[project]
name = "regengine-audit"
version = "0.1.0"
description = "RegEngine AI audit service: append-only SHA-256 hash-chained compliance ledger."
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
EOF

create_file services/audit/Dockerfile <<'EOF'
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8005
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8005"]
EOF

create_file services/audit/README.md <<'EOF'
# Audit Service

Append-only, SHA-256 hash-chained compliance ledger on PostgreSQL --
every compliance evaluation's block hash, previous-hash link, and
payload digest, independently re-verifiable end to end.

Run locally: `uvicorn app.main:app --reload --port 8005`
EOF

# ---------------------------------------------------------------------
# frontend -- React + Tailwind IDE
# ---------------------------------------------------------------------
echo "== frontend =="

create_file frontend/package.json <<'EOF'
{
  "name": "regengine-frontend",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "test": "vitest run",
    "lint": "eslint src"
  },
  "dependencies": {
    "react": "^18.3.1",
    "react-dom": "^18.3.1"
  },
  "devDependencies": {
    "@vitejs/plugin-react": "^4.3.0",
    "autoprefixer": "^10.4.19",
    "eslint": "^9.5.0",
    "postcss": "^8.4.38",
    "tailwindcss": "^3.4.4",
    "vite": "^5.3.1",
    "vitest": "^1.6.0"
  }
}
EOF

create_file frontend/tailwind.config.js <<'EOF'
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{js,jsx,ts,tsx}"],
  theme: { extend: {} },
  plugins: [],
};
EOF

create_file frontend/postcss.config.js <<'EOF'
module.exports = {
  plugins: { tailwindcss: {}, autoprefixer: {} },
};
EOF

create_file frontend/index.html <<'EOF'
<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>RegEngine AI</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.jsx"></script>
  </body>
</html>
EOF

create_file frontend/src/index.css <<'EOF'
@tailwind base;
@tailwind components;
@tailwind utilities;
EOF

create_file frontend/src/main.jsx <<'EOF'
import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App.jsx";
import "./index.css";

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
EOF

create_file frontend/src/App.jsx <<'EOF'
export default function App() {
  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex items-center justify-center">
      <div className="text-center space-y-2">
        <h1 className="text-2xl font-semibold">RegEngine AI</h1>
        <p className="text-slate-400">Compliance IDE scaffold -- replace this with the real workspace UI.</p>
      </div>
    </div>
  );
}
EOF

create_file frontend/Dockerfile <<'EOF'
FROM node:20-slim AS build
WORKDIR /app
COPY package.json ./
RUN npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
EOF

create_file frontend/README.md <<'EOF'
# Frontend

React + Tailwind compliance IDE. Vite-based dev server and build.

Run locally: `npm install && npm run dev`
EOF

# ---------------------------------------------------------------------
# deploy -- Helm charts + Kubernetes manifests
# ---------------------------------------------------------------------
echo "== deploy =="

create_file deploy/helm/regengine/Chart.yaml <<'EOF'
apiVersion: v2
name: regengine
description: RegEngine AI -- SEBI compliance platform (ingestion, agents, compiler, execution, audit, frontend)
type: application
version: 0.1.0
appVersion: "0.1.0"
EOF

create_file deploy/helm/regengine/values.yaml <<'EOF'
image:
  registry: ghcr.io/your-org
  tag: latest
  pullPolicy: IfNotPresent

services:
  ingestion:
    replicas: 2
    port: 8001
  agents:
    replicas: 2
    port: 8002
  compiler:
    replicas: 1
    port: 8003
  execution:
    replicas: 3
    port: 8004
  audit:
    replicas: 2
    port: 8005
  frontend:
    replicas: 2
    port: 80

postgresql:
  enabled: true
  auth:
    database: regengine

redis:
  enabled: true

qdrant:
  enabled: true

opa:
  enabled: true
  image: openpolicyagent/opa:latest-envoy

env:
  HUGGINGFACEHUB_API_TOKEN: ""
  HF_MODEL_ID: "Qwen/Qwen2.5-72B-Instruct"
EOF

create_file deploy/helm/regengine/templates/_helpers.tpl <<'EOF'
{{- define "regengine.fullname" -}}
{{ .Release.Name }}-{{ .Chart.Name }}
{{- end -}}

{{- define "regengine.labels" -}}
app.kubernetes.io/part-of: regengine
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
EOF

create_file deploy/helm/regengine/templates/deployment.yaml <<'EOF'
{{- range $name, $svc := .Values.services }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "regengine.fullname" $ }}-{{ $name }}
  labels:
    {{- include "regengine.labels" $ | nindent 4 }}
    app.kubernetes.io/component: {{ $name }}
spec:
  replicas: {{ $svc.replicas }}
  selector:
    matchLabels:
      app.kubernetes.io/component: {{ $name }}
  template:
    metadata:
      labels:
        app.kubernetes.io/component: {{ $name }}
    spec:
      containers:
        - name: {{ $name }}
          image: "{{ $.Values.image.registry }}/regengine-{{ $name }}:{{ $.Values.image.tag }}"
          imagePullPolicy: {{ $.Values.image.pullPolicy }}
          ports:
            - containerPort: {{ $svc.port }}
          envFrom:
            - configMapRef:
                name: {{ include "regengine.fullname" $ }}-env
{{- end }}
EOF

create_file deploy/helm/regengine/templates/service.yaml <<'EOF'
{{- range $name, $svc := .Values.services }}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "regengine.fullname" $ }}-{{ $name }}
  labels:
    {{- include "regengine.labels" $ | nindent 4 }}
    app.kubernetes.io/component: {{ $name }}
spec:
  selector:
    app.kubernetes.io/component: {{ $name }}
  ports:
    - port: {{ $svc.port }}
      targetPort: {{ $svc.port }}
{{- end }}
EOF

create_file deploy/helm/regengine/templates/configmap.yaml <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "regengine.fullname" . }}-env
data:
  {{- range $key, $value := .Values.env }}
  {{ $key }}: {{ $value | quote }}
  {{- end }}
EOF

create_file deploy/helm/regengine/templates/ingress.yaml <<'EOF'
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {{ include "regengine.fullname" . }}
  annotations:
    kubernetes.io/ingress.class: nginx
spec:
  rules:
    - host: regengine.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {{ include "regengine.fullname" . }}-frontend
                port:
                  number: 80
          - path: /api/execution
            pathType: Prefix
            backend:
              service:
                name: {{ include "regengine.fullname" . }}-execution
                port:
                  number: 8004
EOF

create_file deploy/k8s/namespace.yaml <<'EOF'
apiVersion: v1
kind: Namespace
metadata:
  name: regengine
EOF

create_file deploy/k8s/README.md <<'EOF'
# Kubernetes Manifests

Raw manifests for environments not using the Helm chart in
`deploy/helm/regengine`. `namespace.yaml` creates the `regengine`
namespace every other manifest/the Helm release should target
(`helm install regengine deploy/helm/regengine -n regengine`).
EOF

# ---------------------------------------------------------------------
# Root-level: requirements.txt, pyproject.toml, .env.example, Makefile
# ---------------------------------------------------------------------
echo "== root =="

create_file requirements.txt <<'EOF'
# Root dev/tooling requirements only -- each service under services/*
# pins its OWN runtime dependencies in its own requirements.txt, since
# ingestion/agents/compiler/execution/audit are independently deployed
# services with different (and sometimes conflicting) dependency sets.
# This file is for repo-wide dev tooling: linting, testing, formatting.
pytest>=8.2
pytest-asyncio>=0.23
ruff>=0.5.0
black>=24.4.0
mypy>=1.10
pre-commit>=3.7
EOF

create_file pyproject.toml <<'EOF'
[tool.ruff]
line-length = 120
target-version = "py311"

[tool.black]
line-length = 120
target-version = ["py311"]

[tool.mypy]
python_version = "3.11"
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["services"]
asyncio_mode = "auto"
EOF

create_file .env.example <<'EOF'
# --- Hugging Face / CrewAI (services/agents) ---
HUGGINGFACEHUB_API_TOKEN=
HF_TOKEN=
HF_MODEL_ID=Qwen/Qwen2.5-72B-Instruct

# --- Ingestion (services/ingestion) ---
TIKA_SERVER_URL=http://localhost:9998
QDRANT_URL=http://localhost:6333
QDRANT_API_KEY=

# --- Execution (services/execution) ---
REDIS_URL=redis://localhost:6379/0
OPA_SERVER_URL=http://localhost:8181

# --- Audit (services/audit) ---
LEDGER_DATABASE_URL=postgresql+asyncpg://regengine_ledger_writer:changeme@localhost:5432/regengine

# --- Shared application database (compiler/execution) ---
DATABASE_URL=postgresql+asyncpg://regengine_app:changeme@localhost:5432/regengine

# --- Frontend ---
VITE_API_BASE_URL=http://localhost:8004
EOF

create_file Makefile <<'EOF'
# RegEngine AI -- root Makefile
#
# Targets operate across every service under services/*, plus
# frontend/ and deploy/. Run `make help` for a summary.

SERVICES := ingestion agents compiler execution audit
IMAGE_REGISTRY ?= ghcr.io/your-org
IMAGE_TAG ?= latest
NAMESPACE ?= regengine
HELM_RELEASE ?= regengine

.PHONY: help build test dev deploy clean lint

help:
	@echo "Targets:"
	@echo "  make build   - Build a Docker image for every service + the frontend"
	@echo "  make test    - Run each service's test suite (pytest) and the frontend's (vitest)"
	@echo "  make dev     - Bring up the full local stack via docker compose"
	@echo "  make deploy  - Helm upgrade/install the regengine chart into \$$NAMESPACE"
	@echo "  make lint    - Run ruff/black --check across every Python service"
	@echo "  make clean   - Tear down the local docker compose stack"

build:
	@for svc in $(SERVICES); do \
		echo "==> building services/$$svc"; \
		docker build -t $(IMAGE_REGISTRY)/regengine-$$svc:$(IMAGE_TAG) services/$$svc; \
	done
	@echo "==> building frontend"
	docker build -t $(IMAGE_REGISTRY)/regengine-frontend:$(IMAGE_TAG) frontend

test:
	@for svc in $(SERVICES); do \
		echo "==> testing services/$$svc"; \
		( cd services/$$svc && python -m pytest -q ) || exit 1; \
	done
	@if [ -d frontend/node_modules ]; then \
		echo "==> testing frontend"; \
		( cd frontend && npm test ); \
	else \
		echo "==> skipping frontend tests (run 'npm install' in frontend/ first)"; \
	fi

dev:
	docker compose -f docker-compose.yml up --build

deploy:
	helm upgrade --install $(HELM_RELEASE) deploy/helm/regengine \
		--namespace $(NAMESPACE) --create-namespace \
		--set image.registry=$(IMAGE_REGISTRY) \
		--set image.tag=$(IMAGE_TAG)

lint:
	@for svc in $(SERVICES); do \
		echo "==> linting services/$$svc"; \
		ruff check services/$$svc; \
	done

clean:
	docker compose -f docker-compose.yml down -v
EOF

create_file docker-compose.yml <<'EOF'
# Local dev stack backing `make dev` -- every service + its
# infrastructure dependencies (Postgres, Redis, Qdrant, OPA). Not
# intended for production use; see deploy/helm/regengine for that.
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: regengine
      POSTGRES_USER: regengine_app
      POSTGRES_PASSWORD: changeme
    ports: ["5432:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  qdrant:
    image: qdrant/qdrant:latest
    ports: ["6333:6333"]

  opa:
    image: openpolicyagent/opa:latest
    command: ["run", "--server", "--addr=0.0.0.0:8181"]
    ports: ["8181:8181"]

  ingestion:
    build: ./services/ingestion
    env_file: .env
    ports: ["8001:8001"]
    depends_on: [qdrant]

  agents:
    build: ./services/agents
    env_file: .env
    ports: ["8002:8002"]

  compiler:
    build: ./services/compiler
    env_file: .env
    ports: ["8003:8003"]

  execution:
    build: ./services/execution
    env_file: .env
    ports: ["8004:8004"]
    depends_on: [redis, opa]

  audit:
    build: ./services/audit
    env_file: .env
    ports: ["8005:8005"]
    depends_on: [postgres]

  frontend:
    build: ./frontend
    ports: ["3000:80"]
    depends_on: [execution]

volumes:
  pgdata:
EOF

echo
echo "Done. ${CREATED} file(s) created, ${SKIPPED} file(s) already present and left untouched."
if [ "$SKIPPED" -gt 0 ]; then
  echo "Review the skipped files above if you intended to replace them -- this script never overwrites an existing file."
fi
