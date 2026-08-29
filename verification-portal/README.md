# Custody Chain — Verification Portal

Standalone, offline HTML/WebAssembly tool for SEBI regulatory inspectors
to independently re-verify the cryptographic integrity of exported
RegEngine AI audit-log packages (`regengine-report.py`'s audit binder
ZIPs, or a bare `ledger_proof.json`/`audit_binder.json`), with no
network calls and no dependency on this platform's servers.

Published live at: https://claude.ai/code/artifact/c24470c0-f416-474b-a030-1b9a53a72b7c

## Files

- **`custody-chain.html`** — the full tool: a single self-contained HTML
  file (React 18 UMD + JSZip UMD, both loaded from cdnjs; the
  WebAssembly SHA-256 module below is embedded inline as base64, so no
  separate file needs to ship alongside it). Open it directly in a
  browser — no server, no build step.
- **`sha256.wat`** — hand-written WebAssembly Text source for the
  SHA-256 implementation the tool uses to hash file contents and
  re-derive ledger block hashes. A one-shot `sha256(ptr, len)` API:
  `pad(len)` appends FIPS 180-4 padding in place, `hash(padded_len)`
  runs the compression function and writes the 32-byte digest to a
  fixed output offset. See the comments in the file for the exact
  memory layout.
- **`sha256.wasm`** — the compiled binary (`wat2wasm sha256.wat -o
  sha256.wasm`), ~2KB. This is the exact binary embedded in
  `custody-chain.html`'s `<script id="wasm-b64">` block, base64-encoded.
  Rebuilding from `sha256.wat` reproduces it byte-for-byte (verified
  before committing).
- **`test_sha256_wasm.py`** — the correctness harness used to verify
  this module before embedding it: loads `sha256.wasm` via `wasmtime`
  and compares its output against Python's `hashlib.sha256` across
  boundary-length inputs (0, 55, 56, 57, 63, 64, 65 bytes, a 1MB random
  payload, and a few text fixtures). Run with `pip install wasmtime`
  then `python test_sha256_wasm.py` from this directory.

## Why WebAssembly, not `crypto.subtle.digest`

Browsers already have a native, hardware-accelerated SHA-256
(`crypto.subtle.digest`) — using it would have been simpler. This
module exists specifically because the task asked for a WebAssembly
hashing implementation, and it seemed worth actually building and
proving one correct (via `wat2wasm` + `wasmtime` + `hashlib` comparison)
rather than hand-waving it. `custody-chain.html` also uses the
browser's native WebCrypto for RSA-PSS signature verification — that
one code path was NOT reimplemented, since hand-rolling public-key
cryptography for a security tool is the wrong kind of ambitious.

## Regenerating the WASM binary

```sh
# Requires the WABT toolkit (npm install -g wabt, or your platform's package manager)
wat2wasm sha256.wat -o sha256.wasm

# Re-embed into the HTML tool: base64-encode sha256.wasm and replace the
# contents of the <script id="wasm-b64" type="text/plain"> block in
# custody-chain.html with the result.
```

## Algorithms this tool ports from the live platform

`custody-chain.html`'s verification logic is a faithful port of the
real server-side algorithms, not an independently-invented compatible
format:

- Ledger block hash: `app.ledger.hash_chain.compute_block_hash` —
  `SHA256(previous_hash|payload_digest|sequence_num|evaluated_at)`.
- Manifest file integrity: `regengine-report.py`'s `verify-binder`
  command's per-file SHA-256 check.
- Digital signature: `app.reporting.signing` — RSA-PSS, MGF1(SHA-256),
  salt length `PSS.MAX_LENGTH` (derived dynamically from the embedded
  public key's actual modulus size, not hardcoded to one key size).

One documented limitation: the exported `AuditTrailEntry` schema omits
`clause_hash` and `details`, both inputs to `payload_digest` — this
tool trusts `payload_digest` as given in the export and verifies its
position in the hash chain, rather than re-deriving it from raw fields
that aren't present in the export format today.
