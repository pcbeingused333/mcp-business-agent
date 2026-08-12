variable "region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "name" {
  description = "Base name for every resource."
  type        = string
  default     = "mcp-business-agent"
}

variable "image_tag" {
  description = <<-EOT
    Tag of the image in ECR to deploy.

    Not "latest": Lambda resolves an image tag to a digest at deploy time and
    then pins it, so re-pushing "latest" changes nothing until the function is
    forced to update. An explicit tag per build makes the deployed version
    visible in the plan.
  EOT
  type        = string
  default     = "v1"
}

variable "allowed_hosts" {
  description = <<-EOT
    Value for the server's ALLOWED_HOSTS — the Function URL's hostname.

    Empty on the first apply because the hostname does not exist until the
    Function URL is created, and a resource cannot depend on an attribute of
    something that depends on it. `deploy.sh` runs the second apply with the
    hostname taken from this configuration's own output, which keeps the value
    declarative: Terraform still owns it and there is no out-of-band edit for a
    later apply to revert.

    Empty means the transport rejects every request rather than accepting any
    Host, so a half-finished deploy fails closed.
  EOT
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "Days to keep Lambda logs. Never-expire is the default in AWS and the one real cost risk in an otherwise free-tier stack."
  type        = number
  default     = 14
}
