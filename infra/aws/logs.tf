// Three groups because they have three different audiences: the API's own
// stdout (docs/RUNBOOK.md: "Process logs remain on stdout"), the migration
// task's output, and the host's own agent/boot logs.
//
// Every one of them is CMK-encrypted, and that makes the key POLICY a hard
// dependency rather than a soft one: CloudWatch Logs validates its own access
// to the key inside CreateLogGroup. `kms_key_id` only creates an edge to
// aws_kms_key, so without the depends_on below Terraform is free to create
// these groups while the key still carries its default policy — and the create
// fails. The dependency is on the policy, not the key.

resource "aws_cloudwatch_log_group" "api" {
  name              = "/${var.project}/api"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn

  depends_on = [aws_kms_key_policy.main]
}

resource "aws_cloudwatch_log_group" "migrate" {
  name              = "/${var.project}/migrate"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn

  depends_on = [aws_kms_key_policy.main]
}

resource "aws_cloudwatch_log_group" "host" {
  name              = "/${var.project}/host"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.main.arn

  depends_on = [aws_kms_key_policy.main]
}
