"""Resolves a changed clause's fact fields (or regulatory domain, as a
fallback) into the internal microservices/endpoints predicted to need a
code update -- see service_map.yaml for the operator-maintained mapping
and why it must stay hand-curated rather than LLM-inferred.
"""
from __future__ import annotations

import functools
from pathlib import Path

import yaml

from app.diffing.models import ServiceImpact

_MAP_PATH = Path(__file__).resolve().parent / "service_map.yaml"


@functools.lru_cache(maxsize=1)
def _load_map() -> dict:
    return yaml.safe_load(_MAP_PATH.read_text(encoding="utf-8"))


def resolve_service_impacts(fields: list[str], domain: str | None) -> list[ServiceImpact]:
    """`fields` are `input.facts.<field>` paths as extracted by
    app.diffing.threshold_extraction (or derived fresh from a new
    NumericalThreshold via app.compiler.naming.metric_field_name) --
    stripped of the `facts.` prefix here since service_map.yaml keys on
    the bare field name.

    Returns one ServiceImpact per matched service, deduplicated by name
    (a field appearing twice, or two fields mapping to the same service,
    must not produce two redundant rows for the same clause)."""
    config = _load_map()
    by_field = config.get("by_field", {})
    by_domain = config.get("by_domain", {})

    impacts_by_service: dict[str, ServiceImpact] = {}
    matched_any_field = False

    for field in fields:
        bare_field = field.removeprefix("facts.")
        entry = by_field.get(bare_field)
        if entry is None:
            continue
        matched_any_field = True
        for svc in entry["services"]:
            existing = impacts_by_service.get(svc["name"])
            if existing:
                existing.endpoints = sorted(set(existing.endpoints) | set(svc.get("endpoints", [])))
            else:
                impacts_by_service[svc["name"]] = ServiceImpact(
                    service_name=svc["name"],
                    endpoints=list(svc.get("endpoints", [])),
                    reason=entry["reason"],
                    confidence=1.0,
                )

    if not matched_any_field and domain:
        entry = by_domain.get(domain)
        if entry:
            for svc in entry["services"]:
                impacts_by_service[svc["name"]] = ServiceImpact(
                    service_name=svc["name"],
                    endpoints=list(svc.get("endpoints", [])),
                    reason=entry["reason"],
                    confidence=0.4,  # domain-level fallback, not a specific field match
                )

    return list(impacts_by_service.values())
