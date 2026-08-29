# RegEngine AI — Multi-Region DR Runbook (SEBI BCP)

## Architecture summary

| Component | Primary (ap-south-1, Mumbai) | DR (ap-south-2, Hyderabad) |
|---|---|---|
| Database | Multi-AZ RDS Postgres, synchronous in-region standby | Cross-region async read replica, promotable |
| Audit ledger | Hash-chained rows in the same Postgres (`app.ledger`) | Streamed via replication + independently exported to WORM S3 every 5–15 min |
| Ledger backup | S3 Object Lock (COMPLIANCE, 7yr) | Cross-region replicated copy, own Object Lock |
| DNS | Route53 (or Cloudflare) PRIMARY record, health-checked | SECONDARY record, activated on health-check failure or explicit flip |

Both regions are within India specifically so a full regional failover never creates a SEBI data-localization issue.

## RTO / RPO targets

- **In-region AZ failure**: RTO ~60–120s (RDS Multi-AZ automatic failover), RPO = 0 (synchronous replication).
- **Full regional failure**: RTO target 10–15 min (detection window + promotion + DNS propagation), RPO target ≤30s of ledger writes (bounded by `max_acceptable_lag_seconds` in `dr/failover_orchestrator.py`, which refuses to auto-promote past that threshold).

## Automated failover flow

1. `dr/health_check.py` polls `/healthz` on the primary every 10s; 3 consecutive failures (30s detection window) triggers `dr/failover_orchestrator.run_failover`.
2. Orchestrator checks `ReplicaLag` via CloudWatch. If it exceeds the configured threshold, the run **aborts** (`FailoverAbortedError`, exit code 2) rather than silently accepting unknown data loss — an operator must explicitly pass `--force` and record the accepted RPO in the incident log.
3. `aws rds promote-read-replica` promotes the DR replica; this step is irreversible.
4. DNS is explicitly repointed (`dr/dns_client.py`) as a backstop to Route53/Cloudflare's own health-check-driven failover (covers the gray-failure case where `/healthz` still returns 200 but the DB pool is wedged).
5. `dr/validate_chain_post_failover.py` runs automatically and its result is logged; a failure here does not block the failover (already irreversible by this point) but must page compliance/security immediately.

## Manual DR drill procedure (run at least quarterly per SEBI BCP testing cadence)

1. **Pre-drill**: confirm `dr/ledger_backup_export.py` has run within the last 15 minutes (check for a recent `manifests/manifest_*.json` in the primary WORM bucket) so the fork-detection checkpoint is fresh.
2. **Inject failure**: either `chaos/experiments/scenario3-postgres-failover.yaml` (in-region) for a smaller test, or manually stop the primary RDS instance / block its security group for a full regional test.
3. **Observe detection**: confirm `dr/health_check.py` logs 3 consecutive failures and invokes the orchestrator (or run `dr/failover_orchestrator.py --config dr/failover_config.json` directly for a controlled drill).
4. **Verify promotion**: `aws rds describe-db-instances --db-instance-identifier <dr_replica_id>` shows `StatusInfos` empty and the instance accepting read/write traffic.
5. **Verify DNS**: `dig api.regengine.ai` resolves to the DR ALB within `ttl_seconds` (default 30s) of the flip.
6. **Verify zero/bounded data loss**: compare the last `sequence_num` written on the old primary (from monitoring/logs, if recoverable) against the new primary's max `sequence_num`; the gap should be ≤ what `ReplicaLag` predicted.
7. **Verify chain integrity** (see below).
8. **Post-drill**: fail back deliberately (do not just leave DR as primary) — rebuild the old primary's region as the new DR side via Terraform (`terraform apply` with primary/dr roles swapped, or restore-and-resync), never assume the original primary can silently resume as primary without a fresh base backup.

## Post-failover cryptographic chain validation (SEBI audit requirement)

Run explicitly, or read the orchestrator's automatic run from its logs:

```bash
python dr/validate_chain_post_failover.py \
  --db-url postgresql+asyncpg://regengine_ledger_writer:***@<new-primary-endpoint>/regengine \
  --checkpoint-bucket regengine-prod-ledger-backup-primary \
  --checkpoint-region ap-south-1
```

Two independent checks, both must pass:

- **Internal consistency** (`app.ledger.verifier.verify_chain`): recomputes every `payload_digest`/`current_hash` from `sequence_num=0` (or from a bound anchor) forward. Any recomputed hash mismatch, `previous_hash` mismatch, or sequence gap is a `ChainBreak`.
- **Fork / split-brain detection**: compares the new primary's row at the last WORM-exported checkpoint's `sequence_num` against that checkpoint's recorded `current_hash`. A mismatch means the old primary kept accepting writes after the DR replica's data was captured — the two sides have diverging histories from that point, and this **must** be treated as more severe than an ordinary chain break (it means there may be two valid-looking but mutually inconsistent audit trails).

### If validation fails

1. **Internal break found**: isolate — do not run further writes against the affected range. Pull the corresponding `entries/*.jsonl` object from WORM S3 (immutable, cannot have been altered) and compare row-by-row against what's in the new primary to identify exactly which row(s) changed.
2. **Fork detected**: freeze the new primary (revoke write access at the app layer) and pull both the WORM checkpoint manifest and, if still reachable, the old primary's tail rows past the checkpoint sequence. The two divergent branches must be manually reconciled and reported — this is a SEBI-reportable event, not something to silently resolve by picking one side.
3. In both cases, file an incident with: the `PostFailoverValidationResult` JSON output, the CloudWatch `ReplicaLag` value at time of promotion, and the accepted/actual RPO.

## Ledger backup verification (independent of failover)

Run periodically (weekly) even absent any incident, to confirm the WORM export pipeline itself is trustworthy:

```bash
# 1. Confirm recent exports exist in both regions (CRR working)
aws s3 ls s3://regengine-prod-ledger-backup-primary/manifests/ --region ap-south-1 | tail -5
aws s3 ls s3://regengine-prod-ledger-backup-dr/manifests/ --region ap-south-2 | tail -5

# 2. Confirm an exported entries object's SHA-256 matches its manifest
aws s3api get-object --bucket regengine-prod-ledger-backup-primary --key entries/<range>.jsonl /tmp/entries.jsonl
sha256sum /tmp/entries.jsonl   # compare against the manifest's entries_sha256

# 3. Confirm Object Lock is actually enforced (attempt a delete; must be denied)
aws s3api delete-object --bucket regengine-prod-ledger-backup-primary --key entries/<range>.jsonl
# expect: AccessDenied due to Object Lock COMPLIANCE mode
```
