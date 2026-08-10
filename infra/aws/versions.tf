terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # State holds the RDS master password (see rds.tf for why this deployment
  # generates it rather than delegating to RDS-managed rotation), so the state
  # file is a credential. Configure this backend before the first apply:
  #
  #   terraform init \
  #     -backend-config=bucket=fieldnote-tfstate \
  #     -backend-config=key=prod/terraform.tfstate \
  #     -backend-config=region=us-east-1 \
  #     -backend-config=kms_key_id=<cmk-arn> \
  #     -backend-config=encrypt=true \
  #     -backend-config=use_lockfile=true
  #
  # Left commented so `terraform init` works without a pre-existing bucket while
  # the configuration is being read. Uncomment before you apply anything real.
  #
  # backend "s3" {}
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project
      ManagedBy = "terraform"
      Component = "fieldnote"
    }
  }
}
