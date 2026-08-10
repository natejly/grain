output "api_url" {
  description = "Public base URL of the API. Set NEXT_PUBLIC_API_URL to this in Vercel."
  value       = "https://${var.api_domain}"
}

output "alb_dns_name" {
  description = "Point the api_domain record at this (ALIAS/A for Route 53, CNAME elsewhere)."
  value       = aws_lb.api.dns_name
}

output "alb_zone_id" {
  description = "Hosted zone id for a Route 53 ALIAS record to the load balancer."
  value       = aws_lb.api.zone_id
}

output "ecr_api_repository_url" {
  description = "docker push target for the API image."
  value       = aws_ecr_repository.api.repository_url
}

output "ecr_sandbox_repository_url" {
  description = "docker push target for the sandbox execution image (infra/sandbox/Dockerfile)."
  value       = aws_ecr_repository.sandbox.repository_url
}

output "ecs_cluster_name" {
  description = "Cluster hosting the API service and the migration task."
  value       = aws_ecs_cluster.main.name
}

output "instance_id" {
  description = "The API and sandbox host. Open a shell with: aws ssm start-session --target <this>"
  value       = aws_instance.app.id
}

output "db_endpoint" {
  description = "RDS endpoint. Reachable only from the application security group."
  value       = aws_db_instance.main.address
}

output "objects_bucket" {
  description = "Backup target for OBJECTS_DIR. Not yet the object store itself; see s3.tf."
  value       = aws_s3_bucket.objects.bucket
}

output "secret_names" {
  description = "Secrets Manager entries. Three are placeholders until you write real values."
  value = {
    DATABASE_URL                = aws_secretsmanager_secret.database_url.name
    OPENAI_API_KEY              = aws_secretsmanager_secret.openai_api_key.name
    INTEGRATIONS_ENCRYPTION_KEY = aws_secretsmanager_secret.integrations_key.name
    GOOGLE_LOGIN_CLIENT_SECRET  = aws_secretsmanager_secret.google_login_client_secret.name
  }
}

output "sandbox_image_parameter" {
  description = "SSM parameter the host reads to decide which sandbox image to pull."
  value       = aws_ssm_parameter.sandbox_image.name
}

output "deploy_role_arn" {
  description = "GitHub Actions OIDC deploy role, when enable_github_oidc is set."
  value       = var.enable_github_oidc ? aws_iam_role.deploy[0].arn : ""
}

output "migrate_command" {
  description = "Run the Alembic migration as a one-off task before rolling the service."
  value = join(" ", [
    "aws ecs run-task",
    "--cluster ${aws_ecs_cluster.main.name}",
    "--task-definition ${aws_ecs_task_definition.migrate.family}",
    "--launch-type EC2",
    "--region ${var.aws_region}",
  ])
}
