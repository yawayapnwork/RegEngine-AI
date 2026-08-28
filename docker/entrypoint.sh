#!/bin/sh
# Single dispatch point for every role this one image can run as, selected
# via SERVICE_ROLE. One image/one dependency set for web + worker + beat +
# the one-shot migration job guarantees a worker can never silently run
# against a different library version than the API feeding it.
set -eu

ROLE="${SERVICE_ROLE:-web}"

case "$ROLE" in
  web)
    exec uvicorn app.main:app \
      --host 0.0.0.0 \
      --port "${PORT:-8000}" \
      --workers "${UVICORN_WORKERS:-4}" \
      --proxy-headers
    ;;

  worker)
    # Queue list intentionally excludes regengine_default (no task is ever
    # routed there) unless the operator opts in via CELERY_QUEUES.
    exec celery -A app.execution.celery_app worker \
      -Q "${CELERY_QUEUES:-regengine_batch,regengine_cdc,regengine_webhooks,regengine_ingestion,regengine_agents,regengine_compiler,regengine_vectorstore}" \
      -l "${CELERY_LOG_LEVEL:-info}" \
      --concurrency "${CELERY_CONCURRENCY:-4}"
    ;;

  beat)
    # celery beat must run as exactly one replica cluster-wide -- running
    # two would double-fire the scheduled SEBI ingestion poll. Enforce that
    # at the orchestrator level (docker-compose: one service, no scale;
    # Kubernetes: replicas: 1), not here.
    exec celery -A app.execution.celery_app beat \
      -l "${CELERY_LOG_LEVEL:-info}"
    ;;

  migrate)
    # One-shot: brings the main schema (circulars/clauses/compiled_rules/
    # hitl_reviews + the ledger's FK ref columns) up to head. The audit
    # ledger table itself (append-only triggers, least-privilege grants) is
    # provisioned separately by sql/ledger_schema.sql -- see that file's
    # header for why it is deliberately kept outside Alembic.
    exec alembic upgrade head
    ;;

  *)
    echo "Unknown SERVICE_ROLE: '$ROLE' (expected web|worker|beat|migrate)" >&2
    exit 1
    ;;
esac
