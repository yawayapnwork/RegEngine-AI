variable "name_prefix" {
  type    = string
  default = "regengine-prod"
}

variable "primary_region" {
  type    = string
  default = "ap-south-1"
}

variable "dr_region" {
  type    = string
  default = "ap-south-2"
}

variable "vpc_id_primary" { type = string }
variable "vpc_id_dr" { type = string }
variable "subnet_ids_primary" { type = list(string) }
variable "subnet_ids_dr" { type = list(string) }
variable "app_security_group_ids_primary" { type = list(string) }
variable "app_security_group_ids_dr" { type = list(string) }

variable "kms_key_arn_primary" { type = string }
variable "kms_key_arn_dr" { type = string }

variable "dns_provider" {
  type    = string
  default = "route53"
}
variable "route53_zone_id" {
  type    = string
  default = null
}
variable "cloudflare_zone_id" {
  type    = string
  default = null
}
variable "api_fqdn" {
  type    = string
  default = "api.regengine.ai"
}
variable "primary_alb_dns_name" { type = string }
variable "primary_alb_zone_id" { type = string }
variable "dr_alb_dns_name" { type = string }
variable "dr_alb_zone_id" { type = string }

variable "tags" {
  type = map(string)
  default = {
    Project     = "regengine-ai"
    Environment = "prod"
    ManagedBy   = "terraform"
    Compliance  = "sebi-bcp"
  }
}
