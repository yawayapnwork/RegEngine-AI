output "route53_health_check_id" {
  value = var.dns_provider == "route53" ? aws_route53_health_check.primary[0].id : null
}

output "active_provider" {
  value = var.dns_provider
}
