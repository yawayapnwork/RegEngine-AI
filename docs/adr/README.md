# Architecture Decision Records

Each ADR follows Michael Nygard's format (Status / Context / Decision /
Alternatives Considered / Consequences). Numbered sequentially;
superseding an earlier decision means adding a new ADR that says so,
not editing the old one.

| # | Title | Status |
|---|---|---|
| [0001](0001-opa-rego-vs-jsonlogic-for-legal-rules.md) | OPA Rego as the Canonical Policy Language, with a Restricted JSON-Logic Fallback | Accepted |
| [0002](0002-dual-agent-verification-pattern.md) | Dual-Agent Verification Pattern for Hallucination Prevention | Accepted |
| [0003](0003-sha256-hash-chain-audit-log.md) | SHA-256 Append-Only Hash Chaining for Audit Log Immutability | Accepted |

See also [`docs/architecture/c4-diagrams.md`](../architecture/c4-diagrams.md)
for the C4 model (Context, Container, Component, Code) these decisions
sit within.
