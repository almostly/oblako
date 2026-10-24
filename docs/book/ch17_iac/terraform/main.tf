terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

provider "aws" {}

variable "admin_password" {
  type      = string
  sensitive = true
}

# the address that may connect to Redshift, such as 203.0.113.7/32
variable "allowed_cidr" {
  type = string
}

data "aws_caller_identity" "me" {}

locals {
  account = data.aws_caller_identity.me.account_id
}

resource "aws_s3_bucket" "staging" {
  bucket = "daily-totals-${local.account}"
}

resource "aws_iam_role" "copy" {
  name = "daily-totals-copy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "redshift.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "read_staging" {
  name = "read-staging"
  role = aws_iam_role.copy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:ListBucket"]
      # the staging bucket and the tests' daily-totals-* buckets,
      # in this account only: bucket ARNs carry no account ID
      Resource = [
        "arn:aws:s3:::daily-totals-*",
        "arn:aws:s3:::daily-totals-*/*",
      ]
      Condition = {
        StringEquals = { "aws:ResourceAccount" = local.account }
      }
    }]
  })
}

resource "aws_redshiftserverless_namespace" "ns" {
  namespace_name      = "daily-totals"
  admin_username      = "pipeline_admin"
  admin_user_password = var.admin_password
  db_name             = "dev"
  iam_roles           = [aws_iam_role.copy.arn]
}

resource "aws_security_group" "redshift" {
  name        = "daily-totals-redshift"
  description = "Redshift from one address"
  ingress {
    from_port   = 5439
    to_port     = 5439
    protocol    = "tcp"
    cidr_blocks = [var.allowed_cidr]
  }
}

resource "aws_redshiftserverless_workgroup" "wg" {
  workgroup_name      = "daily-totals"
  namespace_name      = aws_redshiftserverless_namespace.ns.id
  base_capacity       = 8
  publicly_accessible = true
  security_group_ids  = [aws_security_group.redshift.id]
}

output "bucket" {
  value = aws_s3_bucket.staging.bucket
}

output "copy_role_arn" {
  value = aws_iam_role.copy.arn
}

output "redshift_host" {
  value = aws_redshiftserverless_workgroup.wg.endpoint[0].address
}
