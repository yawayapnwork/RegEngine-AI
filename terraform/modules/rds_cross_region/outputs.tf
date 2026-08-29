output "primary_endpoint" {
  value = aws_db_instance.primary.endpoint
}

output "primary_arn" {
  value = aws_db_instance.primary.arn
}

output "dr_replica_endpoint" {
  value = aws_db_instance.dr_replica.endpoint
}

output "dr_replica_id" {
  description = "Instance identifier passed to `aws rds promote-read-replica` by dr/failover_orchestrator.py."
  value       = aws_db_instance.dr_replica.identifier
}

output "dr_replica_arn" {
  value = aws_db_instance.dr_replica.arn
}

output "replica_lag_alarm_arn" {
  value = aws_cloudwatch_metric_alarm.replica_lag.arn
}
