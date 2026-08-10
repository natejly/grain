// One customer-managed key for the whole deployment: RDS storage, the objects
// EBS volume, the S3 backup bucket, ECR layers and every Secrets Manager
// secret. One key rather than five because the IAM story is what matters here —
// with a CMK, "who can read the secrets" is a kms:Decrypt statement scoped by
// kms:ViaService, and that condition is the thing that makes the execution
// role's access to OPENAI_API_KEY a fact rather than an assertion. AWS-managed
// keys cannot carry a key policy you control, so that condition would have
// nowhere to live.
//
// Cost: $1/month plus $0.03 per 10,000 requests. Secrets are read once per task
// start, so the request charge is noise.

resource "aws_kms_key" "main" {
  description             = "${var.project} data at rest"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = { Name = "${var.project}-cmk" }
}

resource "aws_kms_alias" "main" {
  name          = "alias/${var.project}"
  target_key_id = aws_kms_key.main.key_id
}

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

// Three statements, and it is worth being precise about which one is the control.
//
// The root statement is not a grant to a human; it is what makes the key
// administrable and what delegates authorisation to IAM. Removing it or
// narrowing it is the classic way to produce a key nobody can manage. Because
// it delegates to IAM, **the ViaService conditions that actually constrain use
// of this key live in iam.tf**, on each role's kms:Decrypt statement — not
// here. A key policy statement here that re-stated them for the account root
// would be dead weight, since the root statement already permits everything.
//
// The other two are real grants, to service principals that have no IAM
// identity in this account and therefore cannot be reached by the delegation
// above. CloudWatch has to encrypt alarm notifications onto the SNS topic in
// monitoring.tf, and CloudWatch Logs has to encrypt log events for the three
// groups in logs.tf. The Logs one is not optional and not lazy: CreateLogGroup
// with a kmsKeyId calls the key as `logs.<region>.amazonaws.com` and fails the
// apply with InvalidParameterException if the key policy does not name it.
resource "aws_kms_key_policy" "main" {
  key_id = aws_kms_key.main.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "EnableIamPolicyDelegation"
        Effect    = "Allow"
        Principal = { AWS = "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchAlarmsToEncryptedTopic"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action = [
          "kms:GenerateDataKey*",
          "kms:Decrypt",
        ]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        }
      },
      {
        // The third statement, and it is the one that decides whether this
        // configuration applies at all. CloudWatch Logs encrypts a log group
        // as `logs.<region>.amazonaws.com`, a service principal with no IAM
        // identity in this account — so the delegation statement above, which
        // only reaches IAM principals, does not cover it. Without this grant
        // `CreateLogGroup` with a kmsKeyId is rejected outright
        // (InvalidParameterException), and all three groups in logs.tf carry
        // one. This is an apply-time failure, not a runtime one.
        //
        // Scoped by encryption context: CloudWatch Logs passes the log group's
        // own ARN as `aws:logs:arn`, so this key can only ever be used to
        // encrypt groups belonging to this deployment, not any group in the
        // account.
        Sid       = "CloudWatchLogsEncryptsOurGroups"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.aws_region}.amazonaws.com" }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*",
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/${var.project}/*"
          }
        }
      },
    ]
  })
}
