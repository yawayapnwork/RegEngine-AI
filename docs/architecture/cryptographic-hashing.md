# Cryptographic Document Hashing & 8-Stage Provenance Chain

## 1. Executive Summary

RegEngine AI separates document-level and clause-level identity into distinct, unambiguous cryptographic hashes. In regulatory compliance, presenting a hash of extracted or normalized text as the cryptographic hash of the original PDF container is a severe correctness and audit violation. 

Different physical PDF containers (e.g., re-signed documents, altered PDF metadata, non-printing comments, or varying compression levels) can yield identical extracted text while having distinct binary hashes. Conversely, normalizing text collapses whitespace and encoding artifacts, giving a canonical identity to the legal text itself.

Therefore, RegEngine AI explicitly distinguishes:
1. **`source_document_sha256`**: The cryptographic identity of the physical input file (the original PDF container bytes).
2. **`extracted_text_sha256`** (historically `raw_text_digest`): The cryptographic identity of the extracted textual corpus.
3. **`clause.sha256`**: The identity of an individual layout-aware clause.
4. **`payload_digest`** & **`current_hash`**: The chained cryptographic ledger entries.

---

## 2. Hash Definitions & Semantics

| Hash Name | Scope / Target | Computation | Purpose & Guarantees |
| :--- | :--- | :--- | :--- |
| `source_document_sha256` | Original Binary File | `SHA-256(raw_file_bytes)` | Computed immediately upon file receipt prior to any decoding or transformation. Never uses filenames as cryptographic identity. Guarantees that the physical PDF container has not been modified bit-for-bit. |
| `extracted_text_sha256` / `raw_text_digest` | Extracted Text Corpus | `SHA-256(canonicalize(extracted_text))` | Computed on normalized text (NFKC unicode normalization, whitespace collapsing). Guarantees textual identity across different layout engines or PDF encoding variations. |
| `clause.sha256` | Single Clause Block | `SHA-256(circular_number \x1f clause_number \x1f canonicalize(clause_text))` | Identifies a specific regulatory clause scoped to its circular and section number for citation and rule derivation. |
| `payload_digest` | Evaluation Event | `SHA-256(canonical_json(business_fields))` | Fixed deterministic JSON serialization of transaction evaluation outcome and evidence. |
| `current_hash` | Audit Ledger Block | `SHA-256(previous_hash \|\| payload_digest \|\| sequence_num \|\| evaluated_at)` | Merkle-linked cryptographic chain of custody providing tamper-evident append-only journal integrity. |

---

## 3. The 8-Stage Cryptographic Provenance Chain

RegEngine AI links compliance decisions back to original regulatory source documents across an unbroken 8-stage provenance chain:

```
[1. Original PDF]             source_document_sha256 = SHA-256(raw_bytes)
        │
        ▼
[2. Extracted Text]           extracted_text_sha256 = SHA-256(canonical_text)
        │
        ▼
[3. Clause]                   clause.sha256 = SHA-256(circular + clause_no + text)
        │
        ▼
[4. Compiled Rule / Policy]   compiled_rules (rego_policy metadata embeds clause & source hashes)
        │
        ▼
[5. HITL Approval]            hitl_reviews (status = RESOLVED, four-eyes gate satisfied)
        │
        ▼
[6. Evaluation]               PolicyOutcome (decision: allow / deny / flagged)
        │
        ▼
[7. Evidence]                 ComplianceEvaluationEvent.details (snapshot of facts & rule provenance)
        │
        ▼
[8. Ledger]                   compliance_audit_ledger (hash-chained payload_digest & current_hash)
```

1. **Original PDF**: Raw bytes are received and immediately hashed (`source_document_sha256`).
2. **Extracted Text**: The text layer is extracted and canonicalized (`extracted_text_sha256`).
3. **Clause**: Layout-aware chunking segments the document into numbered clauses (`clause.sha256`).
4. **Compiled Rule / Policy**: The compiler generates deterministic Rego / JSON-Logic rules referencing the clause and circular.
5. **HITL Approval**: Required human-in-the-loop review approves the rule, locking its parameters.
6. **Evaluation**: Live transactions evaluate against the active policy.
7. **Evidence**: An immutable snapshot of the facts, violation details, and rule provenance is captured.
8. **Ledger**: The evaluation is committed to the hash-chained audit ledger with cryptographic forward links.

---

## 4. Backward Compatibility & Non-Repurposing of Columns

- **Database Preservation**: The existing column `circulars.raw_text_digest` is NOT repurposed or renamed in the database. It retains its historical meaning as the SHA-256 of the extracted text.
- **New Additive Column**: `circulars.source_document_sha256` and `ingestion_upload_jobs.source_document_sha256` are added via Alembic migration `0008_document_hashing.py`.
- **API Responses**: Both `source_document_sha256` and `extracted_text_sha256` are explicitly exposed on API endpoints alongside `raw_text_digest` and `document_hash`.
