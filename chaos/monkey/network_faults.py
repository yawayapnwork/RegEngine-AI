"""Scenario 2 fault injection: a network dropout mid-transaction during
a `app.ledger.service.LedgerService` write.

Rather than mocking `LedgerService` itself (which would only prove the
mock behaves as configured), this wraps a REAL `AsyncEngine` so one
specific `connection.execute()` call inside the next `begin()`
transaction raises a simulated network fault instead of completing --
everything else (the real SELECT-last-row query, the real INSERT, the
real Postgres advisory lock check, real transaction commit/rollback
semantics) runs unchanged. `LedgerService`/`verify_chain` need no
chaos-specific code path; they just see a connection that dropped.
"""
from __future__ import annotations


class NetworkDropoutFault(ConnectionError):
    """Distinct exception type (not a bare ConnectionResetError) so a
    validator can confirm the failure it observed really was this
    injected fault and not an unrelated database error."""

    def __init__(self, message: str = "Simulated network dropout mid-transaction (chaos.monkey.network_faults).") -> None:
        super().__init__(message)


class _FaultConnectionProxy:
    """Delegates everything to the real connection except `execute`,
    which raises `fault_exc` on its `fail_on_call_index`-th invocation
    (1-indexed) and otherwise behaves exactly like the real connection."""

    def __init__(self, conn, fail_on_call_index: int, fault_exc: Exception) -> None:
        self._conn = conn
        self._fail_on_call_index = fail_on_call_index
        self._fault_exc = fault_exc
        self._call_count = 0

    async def execute(self, *args, **kwargs):
        self._call_count += 1
        if self._call_count == self._fail_on_call_index:
            raise self._fault_exc
        return await self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _FaultBeginContext:
    def __init__(self, engine, fail_on_call_index: int, fault_exc: Exception) -> None:
        self._engine = engine
        self._fail_on_call_index = fail_on_call_index
        self._fault_exc = fault_exc
        self._real_ctx = None

    async def __aenter__(self):
        self._real_ctx = self._engine.begin()
        real_conn = await self._real_ctx.__aenter__()
        return _FaultConnectionProxy(real_conn, self._fail_on_call_index, self._fault_exc)

    async def __aexit__(self, exc_type, exc, tb):
        # Propagates the injected fault (or any other exception) into the
        # real transaction's __aexit__ exactly as SQLAlchemy would see it
        # from a genuine driver-level failure -- this is what actually
        # exercises "does the transaction roll back cleanly", not a
        # chaos-specific shortcut.
        return await self._real_ctx.__aexit__(exc_type, exc, tb)


class NetworkDropoutEngine:
    """Wraps a real `AsyncEngine`. `fail_on_call_index=1` simulates the
    dropout happening on the very first statement of the write
    transaction (the last-row SELECT); `fail_on_call_index=2` simulates
    it happening on the INSERT itself -- i.e. mid-write, after RegEngine
    has already computed the new block's hash and committed to writing
    it, which is the specific moment Requirement 2 names. Note: against
    a Postgres engine, `_acquire_ledger_lock`'s advisory-lock call is
    call #1, so add one to either index there.
    """

    def __init__(self, engine, fail_on_call_index: int = 2, fault_exc: Exception | None = None) -> None:
        self._engine = engine
        self.fail_on_call_index = fail_on_call_index
        self.fault_exc = fault_exc or NetworkDropoutFault()

    def begin(self):
        return _FaultBeginContext(self._engine, self.fail_on_call_index, self.fault_exc)

    def connect(self):
        # Used by app.ledger.verifier.verify_chain for read-only
        # post-mortem verification -- deliberately NOT fault-injected,
        # since the whole point of the check is reading back the ledger
        # AFTER the dropout to confirm its state.
        return self._engine.connect()

    def __getattr__(self, name):
        return getattr(self._engine, name)
