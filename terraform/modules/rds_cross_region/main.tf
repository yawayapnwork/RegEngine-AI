# =============================================================================
# Active-Passive Cross-Region PostgreSQL (Amazon RDS)
# =============================================================================
# Primary: Multi-AZ RDS Postgres in `primary_region`, synchronous standby
#          within-region for zero-data-loss on an AZ failure.
# DR:      Single-AZ cross-region READ REPLICA in `dr_region`, streaming
#          async physical replication. This is the "passive" side: it never
#          takes writes until explicitly promoted (dr/failover_orchestrator.py
#          or `terraform apply -var promote_dr=true`, see outputs.tf note).
#
# Providers: this module expects two aliased providers to be passed in by
# the caller (see terraform/environments/dr/main.tf):
#   provider "aws" { alias = "primary" }
#   provider "aws" { alias = "dr" }
# =============================================================================

terraform {
  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = ">= 5.40"
      configuration_aliases = [aws.primary, aws.dr]
    }
  }
}

# --- Primary region: subnet group, security, Multi-AZ instance ---

resource "aws_db_subnet_group" "primary" {
  provider   = aws.primary
  name       = "${var.name_prefix}-primary-subnets"
  subnet_ids = var.subnet_ids_primary
  tags       = var.tags
}

resource "aws_security_group" "primary_db" {
  provider    = aws.primary
  name        = "${var.name_prefix}-primary-db-sg"
  description = "Postgres primary: inbound 5432 from app/worker security groups only."
  vpc_id      = var.vpc_id_primary

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = var.allowed_security_group_ids_primary
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags
}

resource "aws_db_parameter_group" "primary" {
  provider = aws.primary
  name     = "${var.name_prefix}-primary-pg16"
  family   = "postgres16"

  # wal_level=logical is not required for RDS physical replication, but
  # required if the CDC pipeline (cdc/) is ever pointed at this instance
  # for logical replication slots -- set once here so it doesn't need a
  # disruptive reboot later.
  parameter {
    name  = "wal_level"
    value = "logical"
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "500"
  }

  tags = var.tags
}

resource "aws_db_instance" "primary" {
  provider = aws.primary

  identifier     = "${var.name_prefix}-primary"
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.allocated_storage_gb * 3
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.kms_key_arn_primary

  db_name  = var.db_name
  username = var.master_username
  manage_master_user_password = true

  # Zero-data-loss on an AZ failure: RDS Multi-AZ uses synchronous
  # replication to the standby and fails over automatically in-region
  # (~60-120s) without operator involvement -- this is the first line of
  # defense, before cross-region DR is ever needed.
  multi_az = true

  db_subnet_group_name   = aws_db_subnet_group.primary.name
  vpc_security_group_ids = [aws_security_group.primary_db.id]
  parameter_group_name   = aws_db_parameter_group.primary.name

  backup_retention_period = var.backup_retention_days
  backup_window           = "17:00-17:30" # 22:30-23:00 IST, pre-market
  maintenance_window      = "sun:18:00-sun:19:00"
  copy_tags_to_snapshot   = true

  deletion_protection      = var.deletion_protection
  skip_final_snapshot      = false
  final_snapshot_identifier = "${var.name_prefix}-primary-final"

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  performance_insights_enabled    = true
  performance_insights_kms_key_id = var.kms_key_arn_primary

  tags = var.tags
}

# --- DR region: cross-region read replica (the "passive" side) ---

resource "aws_db_subnet_group" "dr" {
  provider   = aws.dr
  name       = "${var.name_prefix}-dr-subnets"
  subnet_ids = var.subnet_ids_dr
  tags       = var.tags
}

resource "aws_security_group" "dr_db" {
  provider    = aws.dr
  name        = "${var.name_prefix}-dr-db-sg"
  description = "Postgres DR replica: inbound 5432 from app/worker security groups in the DR region."
  vpc_id      = var.vpc_id_dr

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = var.allowed_security_group_ids_dr
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = var.tags
}

resource "aws_db_instance" "dr_replica" {
  provider = aws.dr

  identifier          = "${var.name_prefix}-dr-replica"
  replicate_source_db = aws_db_instance.primary.arn
  instance_class      = var.replica_instance_class

  # Multi-AZ on the replica too: once promoted (becomes the new primary),
  # it must not itself be a single point of failure.
  multi_az = true

  storage_encrypted = true
  kms_key_id        = var.kms_key_arn_dr

  db_subnet_group_name   = aws_db_subnet_group.dr.name
  vpc_security_group_ids = [aws_security_group.dr_db.id]

  # Read replicas cannot set their own backup_retention_period > 0 while
  # still replicating on some engines; RDS Postgres does support it, and we
  # want it non-zero so the replica has its own PITR history *the moment*
  # it is promoted (promotion severs replication -- an unprotected new
  # primary with zero backup history for the first 24h is the classic gap).
  backup_retention_period = var.backup_retention_days
  backup_window           = "17:00-17:30"

  deletion_protection = var.deletion_protection

  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]
  performance_insights_enabled    = true
  performance_insights_kms_key_id = var.kms_key_arn_dr

  tags = merge(var.tags, { Role = "dr-passive-replica" })
}

# --- Replica-lag alarm feeding the health-check/failover scripts' "zero
# data loss" gate: dr/failover_orchestrator.py refuses to promote while
# this metric shows meaningful unreplayed WAL, and instead waits (bounded)
# or escalates for a manual RPO decision. ---
resource "aws_cloudwatch_metric_alarm" "replica_lag" {
  provider            = aws.dr
  alarm_name          = "${var.name_prefix}-dr-replica-lag-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods   = 3
  metric_name          = "ReplicaLag"
  namespace            = "AWS/RDS"
  period               = 60
  statistic            = "Average"
  threshold            = 30 # seconds
  alarm_description    = "Cross-region replica lag exceeds 30s -- investigate before relying on this replica for a zero-data-loss promotion."
  dimensions = {
    DBInstanceIdentifier = aws_db_instance.dr_replica.identifier
  }
  tags = var.tags
}
