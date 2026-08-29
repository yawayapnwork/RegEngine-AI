output "primary_db_endpoint" {
  value = module.rds.primary_endpoint
}

output "dr_replica_endpoint" {
  value = module.rds.dr_replica_endpoint
}

output "dr_replica_id" {
  value = module.rds.dr_replica_id
}

output "ledger_backup_primary_bucket" {
  value = module.ledger_backup.primary_bucket
}

output "ledger_backup_dr_bucket" {
  value = module.ledger_backup.dr_bucket
}
