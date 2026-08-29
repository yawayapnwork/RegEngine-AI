# =============================================================================
# RegEngine AI -- Multi-Region DR Environment (SEBI BCP)
# =============================================================================
# Primary: ap-south-1 (Mumbai). DR: ap-south-2 (Hyderabad) -- both in-country,
# so a full regional failover never puts broker/client data outside India.
# =============================================================================

terraform {
  required_version = ">= 1.7"
  required_providers {
    aws        = { source = "hashicorp/aws", version = ">= 5.40" }
    cloudflare = { source = "cloudflare/cloudflare", version = ">= 4.30" }
  }

  backend "s3" {
    bucket         = "regengine-terraform-state"
    key            = "dr/terraform.tfstate"
    region         = "ap-south-1"
    dynamodb_table = "regengine-terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  alias  = "primary"
  region = var.primary_region
}

provider "aws" {
  alias  = "dr"
  region = var.dr_region
}

module "rds" {
  source = "../../modules/rds_cross_region"
  providers = {
    aws.primary = aws.primary
    aws.dr      = aws.dr
  }

  name_prefix                        = var.name_prefix
  primary_region                     = var.primary_region
  dr_region                          = var.dr_region
  vpc_id_primary                     = var.vpc_id_primary
  vpc_id_dr                          = var.vpc_id_dr
  subnet_ids_primary                 = var.subnet_ids_primary
  subnet_ids_dr                      = var.subnet_ids_dr
  allowed_security_group_ids_primary = var.app_security_group_ids_primary
  allowed_security_group_ids_dr      = var.app_security_group_ids_dr
  kms_key_arn_primary                = var.kms_key_arn_primary
  kms_key_arn_dr                     = var.kms_key_arn_dr
  tags                                = var.tags
}

module "ledger_backup" {
  source = "../../modules/ledger_backup"
  providers = {
    aws.primary = aws.primary
    aws.dr      = aws.dr
  }

  name_prefix          = var.name_prefix
  primary_region       = var.primary_region
  dr_region            = var.dr_region
  kms_key_arn_primary  = var.kms_key_arn_primary
  kms_key_arn_dr       = var.kms_key_arn_dr
  tags                 = var.tags
}

module "dns_failover" {
  source = "../../modules/dns_failover"

  dns_provider             = var.dns_provider
  zone_id                  = var.route53_zone_id
  cloudflare_zone_id       = var.cloudflare_zone_id
  record_name              = var.api_fqdn
  primary_endpoint         = var.primary_alb_dns_name
  primary_endpoint_zone_id = var.primary_alb_zone_id
  dr_endpoint              = var.dr_alb_dns_name
  dr_endpoint_zone_id      = var.dr_alb_zone_id
}
