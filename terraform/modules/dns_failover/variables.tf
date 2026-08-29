variable "dns_provider" {
  description = "\"route53\" or \"cloudflare\". Both create the same failover shape (primary/secondary A-or-CNAME with health-check-driven failover); pick whichever manages regengine's public zone."
  type        = string
  default     = "route53"
  validation {
    condition     = contains(["route53", "cloudflare"], var.dns_provider)
    error_message = "dns_provider must be \"route53\" or \"cloudflare\"."
  }
}

variable "zone_id" {
  description = "Route53 hosted zone id (only used when dns_provider = route53)."
  type        = string
  default     = null
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone id (only used when dns_provider = cloudflare)."
  type        = string
  default     = null
}

variable "record_name" {
  description = "FQDN clients/brokers connect to, e.g. \"api.regengine.ai\"."
  type        = string
}

variable "primary_endpoint" {
  description = "Primary region's ALB/NLB DNS name (Route53) or IP (Cloudflare)."
  type        = string
}

variable "primary_endpoint_zone_id" {
  description = "Route53 only: the primary ALB's own hosted zone id, for the alias record."
  type        = string
  default     = null
}

variable "dr_endpoint" {
  type = string
}

variable "dr_endpoint_zone_id" {
  type    = string
  default = null
}

variable "health_check_path" {
  type    = string
  default = "/healthz"
}

variable "health_check_port" {
  type    = number
  default = 443
}

variable "health_check_interval_seconds" {
  description = "Route53 supports 10s (fast, higher cost) or 30s (standard). 10s keeps detection-to-DNS-flip inside a tight RTO budget."
  type        = number
  default     = 10
}

variable "health_check_failure_threshold" {
  description = "Consecutive failed checks before Route53 marks the primary unhealthy and Route53/Cloudflare start routing new resolutions to the DR endpoint."
  type        = number
  default     = 3
}

variable "ttl_seconds" {
  description = "Low TTL so clients that already resolved the primary IP pick up the failover quickly once their cache expires. This is the dominant term in \"how long until every client is on the new region\", independent of how fast the health check itself fires."
  type        = number
  default     = 30
}
