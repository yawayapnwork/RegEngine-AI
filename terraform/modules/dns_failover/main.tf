# =============================================================================
# DNS Failover: Route 53 (primary implementation) with a Cloudflare variant
# =============================================================================
# Failover routing policy: two records of the same name, PRIMARY and
# SECONDARY, each tied to a health check on the region's own /healthz. As
# long as the primary's health check passes, Route53 answers with the
# primary endpoint exclusively -- the DR endpoint is a live resolver-level
# standby, not something requiring a config push to activate. This is what
# lets `dr/failover_orchestrator.py` treat "flip DNS" as "just let the
# health check keep failing" rather than a record-update API call, though
# the script also does an explicit update for the rare case of a
# gray-failure the health check doesn't catch (e.g. primary answers
# /healthz but the DB connection pool is wedged).
# =============================================================================

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.40"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = ">= 4.30"
    }
  }
}

# --- Route 53 path ---

resource "aws_route53_health_check" "primary" {
  count             = var.dns_provider == "route53" ? 1 : 0
  fqdn              = var.primary_endpoint
  port              = var.health_check_port
  type              = "HTTPS"
  resource_path     = var.health_check_path
  request_interval  = var.health_check_interval_seconds
  failure_threshold = var.health_check_failure_threshold

  tags = {
    Name = "${var.record_name}-primary-health"
  }
}

resource "aws_route53_record" "primary" {
  count           = var.dns_provider == "route53" ? 1 : 0
  zone_id         = var.zone_id
  name            = var.record_name
  type            = "A"
  set_identifier  = "primary"
  failover_routing_policy {
    type = "PRIMARY"
  }
  health_check_id = aws_route53_health_check.primary[0].id

  alias {
    name                   = var.primary_endpoint
    zone_id                = var.primary_endpoint_zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "secondary" {
  count          = var.dns_provider == "route53" ? 1 : 0
  zone_id        = var.zone_id
  name           = var.record_name
  type           = "A"
  set_identifier = "secondary"
  failover_routing_policy {
    type = "SECONDARY"
  }

  alias {
    name                   = var.dr_endpoint
    zone_id                = var.dr_endpoint_zone_id
    evaluate_target_health = false
  }
}

# --- Cloudflare path (alternate provider) ---

resource "cloudflare_load_balancer_pool" "primary" {
  count   = var.dns_provider == "cloudflare" ? 1 : 0
  name    = "${var.record_name}-primary-pool"
  origins {
    name    = "primary"
    address = var.primary_endpoint
    enabled = true
  }
  monitor = cloudflare_load_balancer_monitor.health[0].id
}

resource "cloudflare_load_balancer_pool" "dr" {
  count   = var.dns_provider == "cloudflare" ? 1 : 0
  name    = "${var.record_name}-dr-pool"
  origins {
    name    = "dr"
    address = var.dr_endpoint
    enabled = true
  }
  monitor = cloudflare_load_balancer_monitor.health[0].id
}

resource "cloudflare_load_balancer_monitor" "health" {
  count          = var.dns_provider == "cloudflare" ? 1 : 0
  type           = "https"
  path           = var.health_check_path
  port           = var.health_check_port
  interval       = var.health_check_interval_seconds
  retries        = var.health_check_failure_threshold
  expected_codes = "200"
}

resource "cloudflare_load_balancer" "this" {
  count            = var.dns_provider == "cloudflare" ? 1 : 0
  zone_id          = var.cloudflare_zone_id
  name             = var.record_name
  fallback_pool_id = cloudflare_load_balancer_pool.dr[0].id
  default_pool_ids = [cloudflare_load_balancer_pool.primary[0].id]
  proxied          = true
  ttl              = var.ttl_seconds
  steering_policy  = "off" # deterministic primary-then-fallback, not latency-based
}
