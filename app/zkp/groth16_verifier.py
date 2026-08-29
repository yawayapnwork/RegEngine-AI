"""Pure-Python Groth16 proof verification over BN254 (alt_bn128) -- the
exact curve Circom/snarkjs use by default -- via `py_ecc`. This is the
"verify server-side before writing to the ledger" half of Requirement 2:
no snarkjs/node subprocess, no external service call, just the pairing
equation itself, so a verification result is something RegEngine
computed, not something it took on trust from the same toolchain that
produced the proof.

Verification equation (standard Groth16, matching snarkjs's own
`groth16 verify`):

    e(proof.A, proof.B) == e(vk.alpha, vk.beta) * e(vk_x, vk.gamma) * e(proof.C, vk.delta)

where `vk_x = vk.IC[0] + sum(public_signals[i] * vk.IC[i+1])` folds the
public inputs into a single G1 point via the verification key's `IC`
("input commitment") basis.

`py_ecc.bn128.pairing(Q, P)` computes e(P, Q) (P in G1, Q in G2) via a
Miller loop whose final exponentiation is FQ12.__pow__, implemented as
recursive binary exponentiation over a ~3000-bit exponent
((p^12 - 1) // r). That recursion depth overflows the default native C
thread stack on Windows well before Python's own `sys.recursionlimit`
would stop it (raising `sys.recursionlimit` alone does nothing here --
it caps Python's frame counter, not the OS thread's stack), so every
pairing call in this module runs inside a dedicated worker thread
created with an enlarged `threading.stack_size`. This has been verified
against py_ecc's own bilinearity property (e(a*G1, b*G2) == e(G1,G2)**(a*b))
in this exact sandbox -- see tests/test_zkp.py.
"""
from __future__ import annotations

import sys
import threading
from dataclasses import dataclass

from app.zkp.models import Groth16Proof, Groth16VerificationKey

# 64 MiB is the largest value Windows/CPython accepts here in practice --
# 256 MiB was rejected outright with `ValueError: size not valid`; 64 MiB
# is comfortably enough headroom for the ~3000-bit final-exponentiation
# recursion this module drives. Linux threads default to a much larger
# stack already, so this is a no-op there in all but the most
# stack-constrained containers.
_PAIRING_THREAD_STACK_SIZE = 64 * 1024 * 1024
_PAIRING_RECURSION_LIMIT = 100_000


class ProofVerificationError(ValueError):
    """A proof or verification key was structurally invalid (wrong point
    shape, a coordinate that isn't a valid field element, a public-signal
    count mismatch) -- distinct from a successfully-parsed proof that
    simply fails the pairing check, which is a normal `False` return, not
    an exception."""


@dataclass(frozen=True)
class _CurvePoints:
    alpha1: tuple
    beta2: tuple
    gamma2: tuple
    delta2: tuple
    ic: list[tuple]


def _run_with_enlarged_stack(fn):
    """Runs `fn()` (a zero-arg callable) inside a thread with an enlarged
    native stack and a raised Python recursion limit, per this module's
    docstring. Re-raises whatever `fn` raised, in the calling thread."""
    result: dict = {}

    def _worker() -> None:
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised verbatim in the caller below
            result["error"] = exc

    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, _PAIRING_RECURSION_LIMIT))
    try:
        threading.stack_size(_PAIRING_THREAD_STACK_SIZE)
    except (ValueError, RuntimeError):
        # A platform that rejects this explicit size (observed for larger
        # sizes on Windows) or that has already started threads with a
        # different size this process -- fall back to the default and let
        # the recursion-limit raise still help where it can.
        pass

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    sys.setrecursionlimit(old_limit)

    if "error" in result:
        raise result["error"]
    return result["value"]


def _parse_g1(coords: list[str]):
    from py_ecc.bn128 import FQ

    if len(coords) != 3:
        raise ProofVerificationError(f"G1 point must have 3 coordinates, got {len(coords)}.")
    try:
        x, y = int(coords[0]), int(coords[1])
    except ValueError as exc:
        raise ProofVerificationError(f"G1 coordinates must be decimal integers: {coords[:2]!r}") from exc
    return (FQ(x), FQ(y))


def _parse_g2(coords: list[list[str]]):
    from py_ecc.bn128 import FQ2

    if len(coords) != 3:
        raise ProofVerificationError(f"G2 point must have 3 coordinate pairs, got {len(coords)}.")
    try:
        x = FQ2([int(coords[0][0]), int(coords[0][1])])
        y = FQ2([int(coords[1][0]), int(coords[1][1])])
    except (ValueError, IndexError) as exc:
        raise ProofVerificationError(f"G2 coordinates must be [x0,x1]/[y0,y1] decimal integer pairs: {coords[:2]!r}") from exc
    return (x, y)


def _parse_verification_key(vk: Groth16VerificationKey) -> _CurvePoints:
    if len(vk.IC) != vk.nPublic + 1:
        raise ProofVerificationError(f"verification key declares nPublic={vk.nPublic} but has {len(vk.IC)} IC entries (expected {vk.nPublic + 1}).")
    return _CurvePoints(
        alpha1=_parse_g1(vk.vk_alpha_1),
        beta2=_parse_g2(vk.vk_beta_2),
        gamma2=_parse_g2(vk.vk_gamma_2),
        delta2=_parse_g2(vk.vk_delta_2),
        ic=[_parse_g1(point) for point in vk.IC],
    )


def _compute_vk_x(ic: list[tuple], public_signals: list[int]):
    from py_ecc.bn128 import add, curve_order, multiply

    vk_x = ic[0]
    for i, signal in enumerate(public_signals):
        vk_x = add(vk_x, multiply(ic[i + 1], signal % curve_order))
    return vk_x


def _check_pairing_equation(points: _CurvePoints, pi_a, pi_b, pi_c, public_signals: list[int]) -> bool:
    from py_ecc.bn128 import pairing

    vk_x = _compute_vk_x(points.ic, public_signals)

    lhs = pairing(pi_b, pi_a)  # e(pi_a, pi_b)
    rhs = pairing(points.beta2, points.alpha1) * pairing(points.gamma2, vk_x) * pairing(points.delta2, pi_c)
    return lhs == rhs


def verify_groth16_proof(
    vk: Groth16VerificationKey,
    proof: Groth16Proof,
    public_signals: list[str],
) -> bool:
    """Returns True iff `proof` is a valid Groth16 proof of `vk`'s
    circuit for the given `public_signals`. Raises `ProofVerificationError`
    for structurally malformed input (wrong point shapes, a public-signal
    count that doesn't match `vk.nPublic`) -- never for a proof that
    parses fine but simply doesn't satisfy the pairing equation, which
    returns False like any other failed check."""
    if len(public_signals) != vk.nPublic:
        raise ProofVerificationError(f"Expected {vk.nPublic} public signals for this verification key, got {len(public_signals)}.")

    try:
        signals = [int(s) for s in public_signals]
    except ValueError as exc:
        raise ProofVerificationError(f"public_signals must be decimal-string field elements: {public_signals!r}") from exc

    points = _parse_verification_key(vk)
    pi_a = _parse_g1(proof.pi_a)
    pi_b = _parse_g2(proof.pi_b)
    pi_c = _parse_g1(proof.pi_c)

    return _run_with_enlarged_stack(lambda: _check_pairing_equation(points, pi_a, pi_b, pi_c, signals))
