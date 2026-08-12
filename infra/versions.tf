terraform {
  # 1.10 or newer for use_lockfile below.
  required_version = ">= 1.10"

  # Remote state, because local state means the record of what exists in AWS
  # lives on one laptop. Lose it and Terraform no longer knows about a single
  # resource it created: the next apply tries to build a second copy of
  # everything and fails on the names already taken, and the originals are left
  # running and unmanaged.
  #
  # The bucket is created by bootstrap-state.sh, not by this configuration — a
  # configuration cannot create the bucket its own backend needs in order to
  # run.
  backend "s3" {
    bucket = "mcp-business-agent-tfstate-088970610391"
    key    = "mcp-business-agent/terraform.tfstate"
    region = "us-east-1"

    # Locking against a lock file in the same bucket. The old approach wanted a
    # whole DynamoDB table for this; since 1.10 the bucket is enough.
    use_lockfile = true
    encrypt      = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "mcp-business-agent"
      ManagedBy = "terraform"
    }
  }
}
