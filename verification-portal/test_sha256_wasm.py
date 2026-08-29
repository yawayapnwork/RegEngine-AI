import hashlib
import os
from wasmtime import Store, Module, Instance, Linker, Engine

engine = Engine()
store = Store(engine)
module = Module.from_file(engine, "sha256.wasm")
instance = Instance(store, module, [])
exports = instance.exports(store)
memory = exports["memory"]
pad = exports["pad"]
hash_fn = exports["hash"]

INPUT_PTR = 65536
OUT_PTR = 1024

def wasm_sha256(data: bytes) -> str:
    needed = INPUT_PTR + len(data) + 9 + 63
    current_bytes = memory.size(store) * 65536
    if needed > current_bytes:
        pages_needed = (needed - current_bytes + 65535) // 65536
        memory.grow(store, pages_needed)
    mem = memory.data_ptr(store)
    buf = memory.data_len(store)
    # write bytes into memory via buffer view
    raw = memory.read(store, INPUT_PTR, INPUT_PTR + len(data))
    # use write API instead
    memory.write(store, data, INPUT_PTR)
    padded_len = pad(store, len(data))
    hash_fn(store, padded_len)
    digest = memory.read(store, OUT_PTR, OUT_PTR + 32)
    return digest.hex()

test_cases = [
    b"",
    b"abc",
    b"a" * 55,
    b"a" * 56,
    b"a" * 57,
    b"a" * 63,
    b"a" * 64,
    b"a" * 65,
    b"a" * 1000,
    b"The quick brown fox jumps over the lazy dog",
    os.urandom(1),
    os.urandom(1000000),
    ('{"broker_id":"BRK1","transaction_id":"T1","evaluated_at":"2026-01-01T00:00:00+00:00"}').encode("utf-8"),
]

all_ok = True
for i, data in enumerate(test_cases):
    expected = hashlib.sha256(data).hexdigest()
    actual = wasm_sha256(data)
    ok = expected == actual
    all_ok = all_ok and ok
    label = f"len={len(data)}" if len(data) > 80 else repr(data[:80])
    print(f"[{'OK' if ok else 'FAIL'}] case {i} {label}: expected={expected} actual={actual}")

print("ALL PASS" if all_ok else "SOME FAILED")
