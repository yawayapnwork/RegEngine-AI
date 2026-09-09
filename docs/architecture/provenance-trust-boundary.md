# Cryptographic Provenance & Evidence Trust Boundary

## Overview

RegEngine AI implements an unbroken, mathematically auditable provenance chain linking raw regulatory input to transaction evaluation outcomes. Every stage of regulatory translation and compliance enforcement is sealed using deterministic SHA-256 cryptographic digests and append-only hash chains.

This document explicitly defines **what cryptographic hashing proves**, **what it does not prove**, and **where the trust boundary lies**.

---

## The 11-Stage Conceptual Lineage

$$\begin{aligned}
\text{1. Original PDF SHA-256} &\longrightarrow \text{2. Extracted Text SHA-256} \longrightarrow \text{3. Clause SHA-256} \\
&\longrightarrow \text{4. Canonical Facts Digest} \longrightarrow \text{5. Policy Version + Policy SHA-256} \\
&\longrightarrow \text{6. HITL Review / Approval} \longrightarrow \text{7. Approving Principal & Timestamp} \\
&\longrightarrow \text{8. Transaction Input Digest} \longrightarrow \text{9. OPA Evaluation Result} \\
&\longrightarrow \text{10. Evidence Digest} \longrightarrow \text{11. Ledger Entry / Hash Chain}
\end{aligned}$$

---

## The Trust Boundary: What Hashing Proves vs. Does Not Prove

```
+-----------------------------------------------------------------------------+
| EXTERNAL WORLD (Outside Cryptographic Boundary)                            |
|                                                                             |
|  * SEBI Gazettes & Circular Publications (Is this authentic law?)           |
|  * Compliance Officer Identity & Agency (Is this person authorized/sober?)  |
|  * System Clock / NTP Servers (Has the clock been modified?)                |
+-----------------------------------------------------------------------------+
                                     |
                         [ INGESTION & AUTH BOUNDARY ]
                         Requires: PKI Signatures, FIDO2 MFA, RFC 3161 TSA
                                     |
                                     v
+-----------------------------------------------------------------------------+
| INTERNAL SYSTEM (Inside Cryptographic Boundary - Guaranteed by RegEngine)   |
|                                                                             |
|  * Bit-for-bit unadulterated preservation from PDF to ledger block          |
|  * Deterministic derivation of clauses, canonical facts, and Rego policies  |
|  * Immutable binding of approving principal and policy version              |
|  * Cryptographic binding of transaction facts to evaluated policy version    |
|  * Tamper detection: any bit modified in any artifact breaks verification    |
|  * Append-only ledger ordering (no deletions, forks, or backdated entries)  |
+-----------------------------------------------------------------------------+
```

### 1. What Cryptographic Hashing Proves (Internal Guarantees)

1. **Internal Mathematical Integrity**:
   - The exact binary PDF ingested produced the exact normalized extracted text.
   - The clause chunk matches the source document and was not altered after extraction.
   - The compiled OPA Rego policy matches the audited canonical facts.
   - The evaluation result was produced from the exact transaction input facts against the exact policy version recorded.
2. **Tamper Detection**:
   - If an attacker or rogue administrator modifies a clause, alters a policy threshold, changes a transaction fact, or edits a ledger row after the fact, the recomputed digest immediately fails verification.
3. **Causal Lineage & Multi-Version Immutability**:
   - A later policy version (e.g. version 2) cannot overwrite or alter the provenance chain of an earlier decision made under version 1.
   - Every historical decision permanently references the exact policy version and policy hash that governed it.
4. **Append-Only Sequence Integrity**:
   - The PostgreSQL-native hash-chained ledger (`current_hash = SHA-256(previous_hash || payload_digest || seq || evaluated_at)`, built with a QLDB-journal-inspired design per ADR-0003) prevents retroactive insertion, deletion, or reordering of compliance decisions without invalidating all subsequent blocks, eliminating any dependency on external managed ledger services such as AWS QLDB.

---

### 2. What Hashing Does NOT Prove (External Limits)

Cryptographic hashing alone **does not prove external ground truth authenticity**:

1. **Source Document Authenticity**:
   - A SHA-256 hash of a fraudulent or forged PDF document will verify with 100% mathematical integrity. The hash proves that the system processed *that exact file*, not that SEBI legitimately enacted it.
2. **Human Principal Intent & Non-Repudiation**:
   - Storing a string identifier (e.g. `compliance_officer_id = "officer_alice"`) in a database or ledger proves that the application recorded that identity at approval time. It does not prove that Alice personally reviewed the clause without duress, or that Alice's password/session was not compromised.
3. **Wall-Clock Authority**:
   - A timestamp generated by `datetime.now(timezone.utc)` proves what the server's local clock reported when the record was created. It does not prove true astronomical time if the operating system clock was modified or subject to NTP spoofing.

---

## Production Mitigations for External Authenticity

To extend the trust boundary outward and achieve end-to-end legal authenticity in production environments:

| Boundary Vulnerability | Production Mitigation | Implementation Mechanism |
|---|---|---|
| **PDF Authenticity** | SEBI PKI Digital Signature Validation | Verify X.509 / DSC digital signatures embedded in official SEBI circular PDFs against India PKI Trust Anchors (CCA) before computing `source_document_sha256`. |
| **Human Non-Repudiation** | Hardware-Backed MFA & Digital Signatures | Require FIDO2 / WebAuthn hardware security keys (e.g. YubiKey) and asymmetric digital signing of approval notes by the compliance officer's private key. |
| **Timestamp Authority** | RFC 3161 Cryptographic Time Stamping | Submit `payload_digest` or `current_hash` to an independent RFC 3161 compliant Time Stamping Authority (TSA) or public transparency log (e.g. Sigstore / RFC 6962). |
| **External Auditability** | Public Notarization / Anchor Chaining | Periodically anchor ledger checkpoint hashes to an external immutable ledger or regulatory escrow. |

---

## Compliance Case-Law Memory Agent: Trust Boundary & Invariants

The **Compliance Case-Law Memory Agent** (`app/case_law/`) provides semantic memory over historical, human-resolved regulatory interpretations. Because historical precedent can become stale, biased, or superseded by new statutory amendments, strict architectural invariants govern its trust boundary:

```
+-----------------------------------------------------------------------------------+
| HISTORICAL MEMORY BOUNDARY (Advisory Only)                                        |
|                                                                                   |
|  [ Approved HITL Decisions ]                                                      |
|         |                                                                         |
|         v (Strict Gate: status in [APPROVED, RESOLVED] only)                      |
|  [ Secret Scrubbing: Bearer tokens, API keys, passwords redacted ]                |
|         |                                                                         |
|         v (Provenance Tagging: doc_sha256, clause_sha256, policy_sha256)          |
|  [ Qdrant Vectorstore: case_law_precedents (Strict Tenant Isolation) ]            |
|         |                                                                         |
|         v (Semantic Search: similarity_score >= threshold, tenant_id match)       |
|  [ CaseLawMemoryAgent: Tri-Part Context Assembly ]                                |
|    1. Current Regulatory Source Text   <--- AUTHORITATIVE (Supremacy)             |
|    2. Retrieved Historical Precedents  <--- ADVISORY / UNTRUSTED HISTORICAL DATA   |
|    3. Model Interpretation & Guidance  <--- NON-BINDING SYNTHESIS                 |
|         |                                                                         |
|         +---> Threshold / Condition Conflict Detected?                            |
|                 |                                                                 |
|                 +--- YES ---> Flag Conflict & Escalate to HITL (No Auto-Override) |
|                 +--- NO  ---> Present Advisory Guidance for Human Officer         |
+-----------------------------------------------------------------------------------+
```

### Invariants & Non-Negotiable Guarantees

1. **Current Regulatory Supremacy**:
   - The newly gazetted regulatory text and canonical facts **always override** historical precedent.
   - Precedents cannot alter, loosen, or override current statutory obligations, numeric thresholds, or reporting deadlines.
2. **Strict Indexing Gate (Zero Unverified Bleed)**:
   - Only reviews explicitly stamped `APPROVED` or `RESOLVED` by a verified compliance officer can enter vector memory.
   - Pending reviews, rejected decisions, unverified LLM drafts, and unreviewed pipeline outputs are rejected with hard runtime errors.
3. **Secret & Credential Scrubbing**:
   - Clause text and reviewer notes undergo regex pattern scrubbing for API keys, bearer tokens, passwords, and private keys prior to vector embedding.
4. **Immutable Multi-Dimensional Provenance**:
   - Each precedent record in Qdrant retains full cryptographic lineage: `precedent_id`, `circular_id`, `source_document_sha256`, `clause_sha256`, `policy_version`, `policy_sha256`, `reviewer_id`, `review_notes`, and `approval_timestamp`.
5. **Strict Tenant & Entity Isolation**:
   - Queries and upserts enforce mandatory `tenant_id` filters in Qdrant payload queries. Precedent from Tenant A is invisible and inaccessible to Tenant B.
6. **Untrusted Historical Input**:
   - Retrieved precedent text is treated as untrusted historical data. It is never parsed as executable code, cannot activate policies, and cannot bypass the HITL gate.
7. **Deterministic Conflict Escalation**:
   - If retrieved historical precedent recommends an interpretation or threshold that contradicts current circular text, the agent explicitly flags the discrepancy and escalates to HITL review. Automatic reconciliation is prohibited.

---

## Summary

RegEngine AI's provenance model guarantees that **within the system's operational boundary, no artifact or decision can be secretly modified, backdated, or swapped**. By understanding this trust boundary, compliance and risk teams can confidently verify internal execution integrity while applying appropriate PKI and hardware authentication controls at the boundary.
