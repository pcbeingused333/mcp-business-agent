# ---------------------------------------------------------------------------
# GitHub Actions deploy identity
#
# OIDC rather than an access key in repository secrets. GitHub presents a signed
# token describing which repository and which ref is running, AWS verifies it
# and hands back short-lived credentials. There is no long-lived secret to leak,
# rotate, or find in a fork.
# ---------------------------------------------------------------------------

variable "github_repository" {
  description = "owner/name of the repository allowed to assume the deploy role."
  type        = string
  default     = "pcbeingused333/mcp-business-agent"
}

variable "github_repository_id" {
  description = <<-EOT
    Numeric ID of that repository, from the `repository_id` claim in the token.
    Part of the immutable subject: a repository name freed by a rename or a
    deletion can be claimed by anyone, and an ID cannot be reused.
  EOT
  type        = string
  default     = "1331486528"
}

variable "github_owner_id" {
  description = "Numeric ID of the account, from the `repository_owner_id` claim."
  type        = string
  default     = "82875882"
}

locals {
  owner = split("/", var.github_repository)[0]
  repo  = split("/", var.github_repository)[1]

  # The immutable subject claim. Every guide writes
  # `repo:owner/name:ref:refs/heads/main`, and against this account that form
  # silently never matches: GitHub splices the numeric account and repository
  # IDs into the subject. The assume-role call then fails with "Not authorized
  # to perform sts:AssumeRoleWithWebIdentity", which does not say which
  # condition missed — the claims have to be read out of the token in the
  # workflow to see it.
  github_sub = "repo:${local.owner}@${var.github_owner_id}/${local.repo}@${var.github_repository_id}:ref:refs/heads/main"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # AWS requires the subject to be constrained: a trust policy for this
    # provider that conditions only on other claims is rejected outright with
    # "must evaluate ... token.actions.githubusercontent.com:sub or
    # job_workflow_ref which is not scoped to all". So the subject carries the
    # scoping, and it is matched exactly rather than with a wildcard around the
    # IDs — if GitHub ever changes the format again this breaks loudly instead
    # of quietly widening.
    #
    # It ends in refs/heads/main, not `:*`. The wildcard also matches
    # pull_request runs, and a pull request can come from a fork, so the broad
    # form lets anyone on GitHub open a PR that assumes this role. A pull
    # request's ref is refs/pull/N/merge, which does not match.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = [local.github_sub]
    }
  }
}

resource "aws_iam_role" "github" {
  name               = "${var.name}-github-deploy"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
}

data "aws_iam_policy_document" "github" {
  # Deliberately not enough to run `terraform apply`.
  #
  # Applying this configuration requires creating IAM roles, and a role that can
  # create roles can grant itself anything — so handing that to a public
  # repository's OIDC trust is a privilege escalation waiting to happen. This
  # role can ship a new image and point the function at it, and nothing else.
  # Infrastructure changes are applied from a workstation, where the audit trail
  # is a human. They are also rare; code changes are not.

  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"] # This action does not accept a resource.
  }

  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.this.arn]
  }

  statement {
    sid = "DeployFunctionCode"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:GetFunction", # to wait for the update to settle
      "lambda:GetFunctionConfiguration",
      "lambda:GetFunctionUrlConfig", # the smoke test resolves the endpoint
    ]
    resources = [aws_lambda_function.this.arn]
  }
}

resource "aws_iam_role_policy" "github" {
  role   = aws_iam_role.github.id
  policy = data.aws_iam_policy_document.github.json
}

output "github_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE repository variable in GitHub."
  value       = aws_iam_role.github.arn
}
