"""Settings-driven `circuit_id -> Groth16VerificationKey` lookup, cached
per-process. Mirrors app.execution.dependencies.get_policy_registry's
shape (a small settings-configured registry, loaded lazily and cached)
rather than reading the verification key file on every request.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from app.config import Settings
from app.zkp.models import Groth16VerificationKey

logger = logging.getLogger(__name__)


class UnknownCircuitError(KeyError):
    """Raised when a submission names a `circuit_id` with no configured
    verification key -- a 400, not a 500: the caller asked for a circuit
    this deployment was never told about."""


@lru_cache(maxsize=32)
def _load_verification_key(path: str) -> Groth16VerificationKey:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Groth16VerificationKey.model_validate(raw)


def get_verification_key(settings: Settings, circuit_id: str) -> Groth16VerificationKey:
    path = settings.zkp_verification_keys.get(circuit_id)
    if path is None:
        raise UnknownCircuitError(circuit_id)
    try:
        return _load_verification_key(path)
    except FileNotFoundError as exc:
        # A configured-but-missing key file is an operational error (the
        # deployment declared this circuit_id but never ran
        # zk/scripts/build_circuit.sh's step 4, or shipped it to the
        # wrong path) -- surfaced distinctly from "circuit_id not
        # configured at all" so an operator can tell the two apart.
        logger.error("circuit_id=%s is configured (path=%s) but the verification key file is missing.", circuit_id, path)
        raise UnknownCircuitError(circuit_id) from exc
