// Every secret here maps to exactly one field on apps/api/app/config.py's
// Settings, and the name of the environment variable is the field name
// upper-cased — pydantic-settings does that mapping, there is no aliasing.
//
//   Secret                                Settings field                 Type
//   ------------------------------------  -----------------------------  ----------------------
//   ${project}/database-url               database_url                   str
//   ${project}/openai-api-key             openai_api_key                 Optional[SecretStr]
//   ${project}/integrations-key           integrations_encryption_key    Optional[SecretStr]
//   ${project}/google-login-client-secret google_login_client_secret     Optional[SecretStr]
//
// Everything that is NOT a secret is a plain `environment` entry on the task
// definition (ecs.tf). The split is not stylistic: task-definition environment
// is visible to anyone with ecs:DescribeTaskDefinition, which is a much wider
// set of principals than the four secret ARNs named in the execution role.
//
// Three of the four are created with a placeholder and never written by
// Terraform, so the real values are not in state. Set them once, out of band:
//
//   aws secretsmanager put-secret-value --secret-id ${project}/openai-api-key \
//     --secret-string 'sk-...'
//
//   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' \
//     | xargs -I{} aws secretsmanager put-secret-value \
//         --secret-id ${project}/integrations-key --secret-string '{}'

locals {
  // Long enough that a mistaken `terraform destroy` is recoverable. The
  // integrations key in particular is unrecoverable by any other means: it is
  // the Fernet key services/crypto.py uses, and without it every stored OAuth
  // refresh token and Garmin session token is ciphertext nobody can read.
  secret_recovery_days = 30
}

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.project}/database-url"
  description             = "DATABASE_URL — full SQLAlchemy URL including the RDS master password"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = local.secret_recovery_days
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = local.database_url
}

resource "aws_secretsmanager_secret" "openai_api_key" {
  name                    = "${var.project}/openai-api-key"
  description             = "OPENAI_API_KEY — mandatory; Settings refuses to construct without it"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = local.secret_recovery_days
}

resource "aws_secretsmanager_secret" "integrations_key" {
  name                    = "${var.project}/integrations-key"
  description             = "INTEGRATIONS_ENCRYPTION_KEY — Fernet key for credentials at rest. Losing it is unrecoverable."
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = local.secret_recovery_days
}

resource "aws_secretsmanager_secret" "google_login_client_secret" {
  name                    = "${var.project}/google-login-client-secret"
  description             = "GOOGLE_LOGIN_CLIENT_SECRET — federated sign-in only, separate from the Gmail data client"
  kms_key_id              = aws_kms_key.main.arn
  recovery_window_in_days = local.secret_recovery_days
}

// Placeholders so the ECS task definition has something to reference on the
// first apply. `ignore_changes` means a real value written with
// `put-secret-value` is never reverted by a later `terraform apply`, and the
// value never enters state.
resource "aws_secretsmanager_secret_version" "placeholders" {
  for_each = {
    openai_api_key             = aws_secretsmanager_secret.openai_api_key.id
    integrations_key           = aws_secretsmanager_secret.integrations_key.id
    google_login_client_secret = aws_secretsmanager_secret.google_login_client_secret.id
  }

  secret_id     = each.value
  secret_string = "REPLACE_ME"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

locals {
  // The execution role's read grant is exactly this list. Adding a secret means
  // adding it here, adding it to the task definition's `secrets` block, and
  // nothing else: the IAM policy in iam.tf reads this local.
  secret_arns = [
    aws_secretsmanager_secret.database_url.arn,
    aws_secretsmanager_secret.openai_api_key.arn,
    aws_secretsmanager_secret.integrations_key.arn,
    aws_secretsmanager_secret.google_login_client_secret.arn,
  ]
}
