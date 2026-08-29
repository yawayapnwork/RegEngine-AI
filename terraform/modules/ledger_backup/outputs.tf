output "primary_bucket" {
  value = aws_s3_bucket.ledger_backup_primary.id
}

output "dr_bucket" {
  value = aws_s3_bucket.ledger_backup_dr.id
}

output "primary_bucket_arn" {
  value = aws_s3_bucket.ledger_backup_primary.arn
}

output "dr_bucket_arn" {
  value = aws_s3_bucket.ledger_backup_dr.arn
}
