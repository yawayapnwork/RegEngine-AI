"""Locust load test for RegEngine AI's synchronous OPA evaluation endpoint,
`POST /v1/execution/transactions/evaluate`, across multiple simulated
broker tenants.

Run distributed (a single Python process cannot approach 10,000 rps --
see docker-compose.loadtest.yml and shapes.py's docstring for the math).
Include shapes.py alongside this file so Locust picks up the staged
breakpoint ramp (ActiveLoadShape) instead of a flat -u/-r target:

    locust -f loadtest/locustfile.py,loadtest/shapes.py \
        --host https://loadtest-target.regengine.internal \
        --master --headless --run-time 20m \
        --csv=loadtest/reports/run1 --html=loadtest/reports/run1.html

    # on each worker machine/container:
    locust -f loadtest/locustfile.py,loadtest/shapes.py --worker --master-host <master-ip>

Or use loadtest/run_loadtest.sh, which wraps this plus provisioning and
post-run breakpoint analysis into one command.

Two important latency-measurement notes, both covered in more depth in
breakpoint_analysis.py:

  1. Locust's own percentiles (what --csv/--html report) measure
     END-TO-END client-observed latency: network RTT + TLS + the FastAPI
     request/response cycle, not just the OPA decision query itself. This
     is a real, useful number, but it is NOT what requirement 3's <10ms
     figure is measured against.
  2. The <10ms evaluation-latency SLA is specifically about the OPA
     decision query (`opa_policy_evaluation_duration_seconds`, recorded
     server-side in app.execution.opa_engine.OPAEngine.evaluate) -- that
     is what breakpoint_analysis.py's automated pass/fail gate reads from
     Prometheus, independent of and more precise than anything Locust
     observes from outside the process.
"""
from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path

import yaml
from locust import FastHttpUser, between, constant, events, task

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.execution.models import SourceChannel
from evals.synthetic_trade_generator import ScenarioType, SyntheticTradeGenerator

_CONFIG_PATH = Path(os.environ.get("LOADTEST_TENANTS_CONFIG", Path(__file__).resolve().parent / "tenants_config.yaml"))
_WAIT_TIME_MS = int(os.environ.get("LOADTEST_WAIT_TIME_MS", "0"))  # 0 = closed-loop max-throughput spike mode


def _load_tenants() -> list[dict]:
    data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    return data["tenants"]


_TENANTS = _load_tenants()
_TENANT_WEIGHTS = [t["weight"] for t in _TENANTS]


def _pick_tenant() -> dict:
    return random.choices(_TENANTS, weights=_TENANT_WEIGHTS, k=1)[0]


@events.init_command_line_parser.add_listener
def _add_arguments(parser) -> None:
    parser.add_argument(
        "--scenario-mix",
        type=str,
        default="mixed_market_stream",
        help="evals.synthetic_trade_generator.ScenarioType value to draw trade payloads from.",
    )


class BrokerEvaluationUser(FastHttpUser):
    """One simulated broker-side OMS/RMS system integration, authenticated
    as one tenant for its entire lifetime (matching how a real broker
    integration holds a long-lived service credential, not one token per
    trade)."""

    # 0ms wait_time by default: this is a spike/breakpoint test simulating
    # peak market-open order flow, where a broker's OMS fires the next
    # order the instant the previous one's decision comes back, not a
    # human-paced workflow. Set LOADTEST_WAIT_TIME_MS for a steady-state
    # baseline run instead of a breakpoint run.
    wait_time = constant(_WAIT_TIME_MS / 1000.0) if _WAIT_TIME_MS else between(0, 0)

    def on_start(self) -> None:
        self.tenant = _pick_tenant()
        self.generator = SyntheticTradeGenerator()
        self.scenario = ScenarioType(self.environment.parsed_options.scenario_mix) if hasattr(self.environment, "parsed_options") else ScenarioType.MIXED_MARKET_STREAM
        self._trade_seq = 0
        self.access_token: str | None = None
        self.token_expires_at: float = 0.0
        self._authenticate()

    def _authenticate(self) -> None:
        resp = self.client.post(
            "/v1/auth/token",
            json={"client_id": self.tenant["client_id"], "client_secret": self.tenant["client_secret"]},
            name="/v1/auth/token",
        )
        if resp.status_code != 200:
            # Fail loudly and stop this simulated user rather than
            # generating a flood of 401s that would pollute the evaluate
            # endpoint's own latency/error stats with an unrelated
            # provisioning problem -- see loadtest/provision_tenants.py.
            self.environment.runner.quit()
            raise RuntimeError(
                f"Auth failed for tenant {self.tenant['tenant_id']} (status={resp.status_code}, body={resp.text[:300]!r}). "
                f"Did you run loadtest/provision_tenants.py against this target first?"
            )
        body = resp.json()
        self.access_token = body["access_token"]
        # Refresh a little early (90% of TTL) so we never fire a request on
        # a token that expires mid-flight.
        self.token_expires_at = time.time() + body["expires_in"] * 0.9

    def _headers(self) -> dict[str, str]:
        if time.time() >= self.token_expires_at:
            self._authenticate()
        return {"Authorization": f"Bearer {self.access_token}"}

    @task
    def evaluate_transaction(self) -> None:
        self._trade_seq += 1
        trade = self.generator.generate_trade(self._trade_seq, self.scenario)
        payload = dict(trade.payload)
        # Force the payload's broker_id to match this simulated user's own
        # authenticated tenant -- app.api.execution_routes rejects a
        # Broker_API_Client submitting on behalf of a different tenant_id
        # (403), and that check firing under load would misrepresent a
        # test-harness bug as a system failure.
        payload["broker_id"] = self.tenant["tenant_id"]
        payload["source_channel"] = SourceChannel.REST_SYNC.value

        with self.client.post(
            "/v1/execution/transactions/evaluate",
            json=payload,
            headers=self._headers(),
            name="/v1/execution/transactions/evaluate",
            catch_response=True,
        ) as response:
            if response.status_code == 401:
                # Token expired/revoked mid-run: re-auth and let Locust
                # record this one as a failure (it is, from the caller's
                # perspective) rather than silently retrying inline and
                # masking a real auth problem.
                response.failure("401 Unauthorized -- re-authenticating for next request")
                self._authenticate()
                return
            if response.status_code != 200:
                response.failure(f"Unexpected status {response.status_code}: {response.text[:300]}")
                return

            body = response.json()
            if body.get("decision") not in ("allow", "deny", "flagged"):
                response.failure(f"Malformed EvaluationResult body: {body}")
