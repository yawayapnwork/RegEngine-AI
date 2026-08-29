# =============================================================================
# Hash-Chained Ledger Backup: WORM storage + cross-region replication
# =============================================================================
# RegEngine AI's audit ledger (app/ledger) is a hash-chained Postgres table,
# not a managed ledger database like AWS QLDB -- it gets QLDB's tamper-
# evidence property (any alteration breaks recomputable hashes, see
# app/ledger/hash_chain.py and verify_chain) from the SHA-256 chain itself,
# not from the storage engine. What QLDB would additionally buy you is an
# independently-immutable copy outside the database -- that's what this
# module builds: `dr/ledger_backup_export.py` periodically exports ledger
# rows plus their computed hashes into Object-Lock (WORM) S3, replicated
# cross-region, so a compromised or failed-over Postgres can never be the
# only place the chain's history exists.
#
# Object Lock requires versioning enabled on the bucket, and the
# destination bucket for CRR must also have Object Lock enabled -- both are
# configured below.
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

resource "aws_s3_bucket" "ledger_backup_primary" {
  provider      = aws.primary
  bucket        = "${var.name_prefix}-ledger-backup-primary"
  object_lock_enabled = true
  tags          = var.tags
}

resource "aws_s3_bucket_versioning" "ledger_backup_primary" {
  provider = aws.primary
  bucket   = aws_s3_bucket.ledger_backup_primary.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "ledger_backup_primary" {
  provider = aws.primary
  bucket   = aws_s3_bucket.ledger_backup_primary.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.object_lock_retention_days
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ledger_backup_primary" {
  provider = aws.primary
  bucket   = aws_s3_bucket.ledger_backup_primary.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn_primary
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "ledger_backup_primary" {
  provider                = aws.primary
  bucket                  = aws_s3_bucket.ledger_backup_primary.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket" "ledger_backup_dr" {
  provider            = aws.dr
  bucket              = "${var.name_prefix}-ledger-backup-dr"
  object_lock_enabled = true
  tags                = var.tags
}

resource "aws_s3_bucket_versioning" "ledger_backup_dr" {
  provider = aws.dr
  bucket   = aws_s3_bucket.ledger_backup_dr.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "ledger_backup_dr" {
  provider = aws.dr
  bucket   = aws_s3_bucket.ledger_backup_dr.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      days = var.object_lock_retention_days
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ledger_backup_dr" {
  provider = aws.dr
  bucket   = aws_s3_bucket.ledger_backup_dr.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn_dr
    }
    bucket_key_enabled = true
  }
}

resource "aws_iam_role" "replication" {
  provider = aws.primary
  name     = "${var.name_prefix}-ledger-backup-replication"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "s3.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "replication" {
  provider = aws.primary
  name     = "${var.name_prefix}-ledger-backup-replication"
  role     = aws_iam_role.replication.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetReplicationConfiguration", "s3:ListBucket"]
        Resource = [aws_s3_bucket.ledger_backup_primary.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObjectVersionForReplication", "s3:GetObjectVersionAcl", "s3:GetObjectVersionTagging"]
        Resource = ["${aws_s3_bucket.ledger_backup_primary.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ReplicateObject", "s3:ReplicateDelete", "s3:ReplicateTags", "s3:ObjectOwnerOverrideToBucketOwner"]
        Resource = ["${aws_s3_bucket.ledger_backup_dr.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [var.kms_key_arn_primary]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Encrypt"]
        Resource = [var.kms_key_arn_dr]
      }
    ]
  })
}

resource "aws_s3_bucket_replication_configuration" "ledger_backup" {
  provider   = aws.primary
  role       = aws_iam_role.replication.arn
  bucket     = aws_s3_bucket.ledger_backup_primary.id
  depends_on = [aws_s3_bucket_versioning.ledger_backup_primary, aws_s3_bucket_versioning.ledger_backup_dr]

  rule {
    id     = "ledger-chain-crr"
    status = "Enabled"

    destination {
      bucket        = aws_s3_bucket.ledger_backup_dr.arn
      storage_class = "STANDARD_IA"
      encryption_configuration {
        replica_kms_key_id = var.kms_key_arn_dr
      }
    }

    source_selection_criteria {
      sse_kms_encrypted_objects {
        status = "Enabled"
      }
    }

    delete_marker_replication {
      status = "Disabled" # WORM: a delete marker must never propagate and shadow an immutable original
    }
  }
}
