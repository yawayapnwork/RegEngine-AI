variable "name_prefix" {
  type = string
}

variable "primary_region" {
  type    = string
  default = "ap-south-1"
}

variable "dr_region" {
  type    = string
  default = "ap-south-2"
}

variable "object_lock_retention_days" {
  description = "WORM retention for exported ledger chain snapshots, in COMPLIANCE mode -- not even the account root can shorten or delete within this window. SEBI record-keeping rules require multi-year retention for compliance evaluation records; default here is 7 years."
  type        = number
  default     = 2557
}

variable "kms_key_arn_primary" {
  type = string
}

variable "kms_key_arn_dr" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}
