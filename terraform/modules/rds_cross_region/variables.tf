variable "name_prefix" {
  description = "Prefix for all resource names, e.g. \"regengine-prod\"."
  type        = string
}

variable "primary_region" {
  description = "AWS region hosting the active-primary RDS instance. Default keeps data in-country (Mumbai) for SEBI data-localization."
  type        = string
  default     = "ap-south-1"
}

variable "dr_region" {
  description = "AWS region hosting the cross-region read replica. ap-south-2 (Hyderabad) is used instead of an out-of-country region so DR failover never triggers a data-residency violation."
  type        = string
  default     = "ap-south-2"
}

variable "engine_version" {
  type    = string
  default = "16.4"
}

variable "instance_class" {
  type    = string
  default = "db.r6g.xlarge"
}

variable "replica_instance_class" {
  description = "Usually matched to instance_class so the replica can absorb full primary throughput immediately after promotion -- undersizing the replica silently caps your real RTO."
  type        = string
  default     = "db.r6g.xlarge"
}

variable "allocated_storage_gb" {
  type    = number
  default = 200
}

variable "master_username" {
  type    = string
  default = "regengine_admin"
}

variable "db_name" {
  type    = string
  default = "regengine"
}

variable "vpc_id_primary" {
  type = string
}

variable "vpc_id_dr" {
  type = string
}

variable "subnet_ids_primary" {
  type = list(string)
}

variable "subnet_ids_dr" {
  type = list(string)
}

variable "allowed_security_group_ids_primary" {
  description = "Security groups (app/worker nodes) allowed to reach the primary on 5432."
  type        = list(string)
}

variable "allowed_security_group_ids_dr" {
  type = list(string)
}

variable "backup_retention_days" {
  description = "PITR window on the primary. SEBI BCP guidance expects at minimum a 7-year audit trail for the ledger (handled separately by the WORM export in the ledger_backup module) but PITR itself only needs to cover the realistic operational recovery window."
  type        = number
  default     = 35
}

variable "kms_key_arn_primary" {
  description = "Customer-managed KMS key in the primary region. Cross-region replicas cannot share a KMS key across regions -- a second key (kms_key_arn_dr) is required and RDS re-encrypts during replication."
  type        = string
}

variable "kms_key_arn_dr" {
  type = string
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
