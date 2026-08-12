locals {
  image_uri = "${aws_ecr_repository.this.repository_url}:${var.image_tag}"
}

# ---------------------------------------------------------------------------
# Image registry
# ---------------------------------------------------------------------------

resource "aws_ecr_repository" "this" {
  name = var.name

  # Every other resource here is free-tier forever; ECR storage is only free for
  # the first 500 MB and this image is ~800 MB uncompressed. Keeping the
  # repository to a couple of images is what keeps the bill in cents.
  force_delete = true

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name

  # Untagged images are the previous build's layers left behind by a re-push of
  # the same tag. Nothing references them and they are the bulk of the storage.
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after a day"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 1 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep only the three most recent tagged images"
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 3 }
        action       = { type = "expire" }
      },
    ]
  })
}

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "ops" {
  name = "business-ops"

  # On-demand, not provisioned: a portfolio demo's traffic is a handful of
  # requests when somebody opens the link and nothing for days in between.
  # Provisioned capacity bills for the idle time.
  billing_mode = "PAY_PER_REQUEST"

  hash_key  = "PK"
  range_key = "SK"

  attribute {
    name = "PK"
    type = "S"
  }

  attribute {
    name = "SK"
    type = "S"
  }

  point_in_time_recovery {
    # The table's entire contents are regenerable with a seed invocation, so
    # paying for continuous backups would buy nothing.
    enabled = false
  }
}

# ---------------------------------------------------------------------------
# Function
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "lambda" {
  # Deliberately not AWSLambdaBasicExecutionRole: that grants logs:* on every
  # log group in the account. This function writes to exactly one.
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }

  statement {
    sid = "OpsTable"
    actions = [
      "dynamodb:DescribeTable", # table.load() on the health check
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:BatchWriteItem",
      "dynamodb:TransactWriteItems",
    ]
    resources = [aws_dynamodb_table.ops.arn]
  }
}

resource "aws_iam_role_policy" "lambda" {
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

resource "aws_cloudwatch_log_group" "lambda" {
  # Declared rather than left to Lambda's implicit creation, which has no
  # retention and keeps every log line forever.
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "this" {
  function_name = var.name
  role          = aws_iam_role.lambda.arn

  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["x86_64"]

  # The MCP server is I/O-bound on DynamoDB, but Lambda scales CPU with memory
  # and cold start is mostly interpreter and import time, so the larger size is
  # frequently cheaper as well as faster: it bills for less duration.
  memory_size = 512
  timeout     = 30

  environment {
    variables = {
      OPS_TABLE     = aws_dynamodb_table.ops.name
      ALLOWED_HOSTS = var.allowed_hosts
      LOG_LEVEL     = "INFO"
    }
  }

  depends_on = [
    aws_iam_role_policy.lambda,
    aws_cloudwatch_log_group.lambda,
  ]
}

resource "aws_lambda_function_url" "this" {
  function_name = aws_lambda_function.this.function_name

  # A public demo an MCP client can point at without credentials. The tools
  # operate on a seeded fictional business, so the data is disclosable by
  # construction; nothing here is worth authenticating.
  #
  # What that costs: anyone can invoke it, and invocations are billable. The
  # $5 budget alarm is the backstop, and the free tier is a million requests a
  # month.
  authorization_type = "NONE"
}
