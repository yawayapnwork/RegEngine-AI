#!/usr/bin/env bash
# End-to-end breakpoint load test: provision tenants -> run headless
# distributed Locust -> query Prometheus for the hard SLA gates ->
# render an HTML report.
#
# Usage:
#   ./loadtest/run_loadtest.sh <target-host> <target-redis-url> <prometheus-url> <run-id>
#
# Example:
#   ./loadtest/run_loadtest.sh https://staging.regengine.internal \
#       redis://staging-redis:6379/0 http://localhost:9090 breakpoint-2026-08-29

set -euo pipefail

TARGET_HOST="${1:?Usage: run_loadtest.sh <target-host> <target-redis-url> <prometheus-url> <run-id>}"
TARGET_REDIS_URL="${2:?Missing target-redis-url}"
PROMETHEUS_URL="${3:?Missing prometheus-url}"
RUN_ID="${4:-run-$(date -u +%Y%m%dT%H%M%SZ)}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPORTS_DIR="$SCRIPT_DIR/reports"
CSV_PREFIX="$REPORTS_DIR/$RUN_ID"

mkdir -p "$REPORTS_DIR"

echo "[1/4] Provisioning load-test tenants against $TARGET_REDIS_URL ..."
python "$SCRIPT_DIR/provision_tenants.py" --redis-url "$TARGET_REDIS_URL"

echo "[2/4] Running headless distributed Locust breakpoint test against $TARGET_HOST ..."
WINDOW_START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

LOADTEST_SHAPE="${LOADTEST_SHAPE:-breakpoint}" locust -f "$SCRIPT_DIR/locustfile.py,$SCRIPT_DIR/shapes.py" \
  --host "$TARGET_HOST" \
  --headless \
  --csv "$CSV_PREFIX" \
  --html "$CSV_PREFIX.html" \
  --logfile "$CSV_PREFIX.log"

WINDOW_END="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "[3/4] Running automated breakpoint pass/fail analysis against $PROMETHEUS_URL ..."
set +e
python "$SCRIPT_DIR/breakpoint_analysis.py" \
  --prometheus-url "$PROMETHEUS_URL" \
  --window-start "$WINDOW_START" \
  --window-end "$WINDOW_END" \
  --locust-csv-prefix "$CSV_PREFIX" \
  --out "${CSV_PREFIX}_breakpoint_result.json"
ANALYSIS_EXIT_CODE=$?
set -e

echo "[4/4] Rendering HTML performance report ..."
python "$SCRIPT_DIR/report_generator.py" \
  --result "${CSV_PREFIX}_breakpoint_result.json" \
  --out "${CSV_PREFIX}_report.html" \
  --run-id "$RUN_ID"

echo "Report: ${CSV_PREFIX}_report.html"
exit "$ANALYSIS_EXIT_CODE"
