"""Publishes a candidate policy to the SAME production OPA server under
a namespaced package/rule_id, so it can be evaluated read-only
alongside production without ever being reachable at production's own
package path -- the exact rewrite-the-`package`-line trick
`app.execution.tenant_policy_registry.TenantOPAEngine` already uses for
tenant isolation, applied here for canary isolation instead.
"""
from __future__ import annotations

from app.compiler.models import CompiledRego
from app.execution.opa_engine import OPAEngine


def canary_opa_rule_id(canary_id: str, rule_id: str) -> str:
    return f"canary_{canary_id}_{rule_id}"


def canary_package(canary_id: str, package: str) -> str:
    return f"canary.{canary_id.replace('-', '_')}.{package}"


def _rewrite_package_declaration(rego_code: str, new_package: str) -> str:
    rewritten_lines = []
    for line in rego_code.splitlines():
        if line.strip().startswith("package "):
            rewritten_lines.append(f"package {new_package}")
        else:
            rewritten_lines.append(line)
    return "\n".join(rewritten_lines)


class CanaryOPAPublisher:
    """Thin wrapper over a plain `OPAEngine` pointed at the production
    OPA server -- unlike `TenantOPAEngine`, this does NOT need its own
    HTTP plumbing, since canary namespacing is pure string rewriting on
    top of the exact same `publish_policy`/`remove_policy`/`evaluate`
    calls `OPAEngine` already makes."""

    def __init__(self, opa_engine: OPAEngine) -> None:
        self._opa = opa_engine

    async def publish_candidate(self, canary_id: str, candidate: CompiledRego) -> tuple[str, str]:
        """Returns (namespaced_rule_id, namespaced_package) -- both
        recorded on the `CanaryRun` so later evaluate/remove calls use
        the exact same identifiers."""
        namespaced_rule_id = canary_opa_rule_id(canary_id, candidate.rule_id)
        namespaced_package = canary_package(canary_id, candidate.package)
        namespaced = candidate.model_copy(update={
            "rule_id": namespaced_rule_id,
            "package": namespaced_package,
            "rego_code": _rewrite_package_declaration(candidate.rego_code, namespaced_package),
        })
        await self._opa.publish_policy(namespaced)
        return namespaced_rule_id, namespaced_package

    async def remove_candidate(self, namespaced_rule_id: str) -> None:
        await self._opa.remove_policy(namespaced_rule_id)
