output "region" {
  value = var.region
}

output "repository_url" {
  description = "ECR repository to push the image to."
  value       = aws_ecr_repository.this.repository_url
}

output "image_uri" {
  description = "Exact image the function is pinned to."
  value       = local.image_uri
}

output "function_name" {
  value = aws_lambda_function.this.function_name
}

output "function_url" {
  description = "Public MCP endpoint. The server is mounted at /mcp."
  value       = aws_lambda_function_url.this.function_url
}

output "mcp_endpoint" {
  description = "What to hand an MCP client."
  value       = "${trimsuffix(aws_lambda_function_url.this.function_url, "/")}/mcp"
}

output "function_url_host" {
  description = "Feeds back in as the allowed_hosts variable on the second apply."
  value       = replace(replace(aws_lambda_function_url.this.function_url, "https://", ""), "/", "")
}

output "table_name" {
  value = aws_dynamodb_table.ops.name
}
