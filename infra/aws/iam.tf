// Four roles, and the interesting one is the empty one.
//
// An honest statement of what these boundaries are worth first, because
// "least privilege" without it is decoration. The API container is bind-mounted
// /var/run/docker.sock — that is how the `container` sandbox driver starts
// sibling containers — and a process that can talk to the Docker socket can
// start a privileged container and become root on the host. So role separation
// here is NOT a boundary against an attacker who already has code execution in
// the API container; it is a boundary against everything short of that
// (a misconfigured deploy, a leaked task-definition dump, an over-broad
// third-party action) and it is what keeps the blast radius of a *sandbox*
// escape from including the deployment's AWS credentials.
//
// What actually protects the credentials from generated code is not IAM at all:
// sandbox containers run with `--network none`, so they cannot reach
// 169.254.169.254 or 169.254.170.2. There is no route to a credential endpoint,
// which is a stronger claim than any policy document.

// --- 1. EC2 instance role --------------------------------------------------
// What the ECS agent needs and nothing else. Notably absent versus the
// AWS-managed AmazonEC2ContainerServiceforEC2Role: `ecs:CreateCluster`, because
// the cluster is a Terraform resource and an instance that can create clusters
// can register itself somewhere nobody is looking.

resource "aws_iam_role" "ecs_instance" {
  name = "${var.project}-ecs-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_instance_profile" "ecs_instance" {
  name = "${var.project}-ecs-instance"
  role = aws_iam_role.ecs_instance.name
}

resource "aws_iam_role_policy" "ecs_instance_agent" {
  name = "ecs-agent"
  role = aws_iam_role.ecs_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        // Every ECS action that accepts the ecs:cluster condition key is
        // pinned to this deployment's cluster.
        Sid    = "AgentScopedToThisCluster"
        Effect = "Allow"
        Action = [
          "ecs:DeregisterContainerInstance",
          "ecs:RegisterContainerInstance",
          "ecs:UpdateContainerInstancesState",
          "ecs:Poll",
          "ecs:StartTelemetrySession",
          "ecs:SubmitAttachmentStateChanges",
          "ecs:SubmitContainerStateChange",
          "ecs:SubmitTaskStateChange",
        ]
        Resource = "*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.main.arn }
        }
      },
      {
        // These two accept no resource and no condition. Called out rather than
        // folded into the statement above so the unavoidable wildcard is
        // visible instead of implied.
        Sid    = "AgentUnscopeable"
        Effect = "Allow"
        Action = [
          "ecs:DiscoverPollEndpoint",
          "ec2:DescribeTags",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "ecs_instance_ecr" {
  name = "ecr-pull"
  role = aws_iam_role.ecs_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        // GetAuthorizationToken is account-wide by design: it returns a token,
        // not image bytes. The pull itself is scoped to the two repositories.
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "PullOurImagesOnly"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
        ]
        Resource = [
          aws_ecr_repository.api.arn,
          aws_ecr_repository.sandbox.arn,
        ]
      },
      {
        // Layers are KMS-encrypted (ecr.tf), so a pull needs Decrypt — through
        // ECR and only through ECR.
        Sid      = "DecryptImageLayers"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = aws_kms_key.main.arn
        Condition = {
          StringEquals = { "kms:ViaService" = "ecr.${var.aws_region}.amazonaws.com" }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "ecs_instance_backup" {
  name = "objects-backup"
  role = aws_iam_role.ecs_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        // The nightly `aws s3 sync` of /var/lib/grain/objects. Listing is
        // bucket-level and cannot be narrowed below the bucket ARN, so it is
        // narrowed by prefix condition instead.
        Sid      = "ListBackupPrefix"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.objects.arn
        Condition = {
          StringLike = { "s3:prefix" = ["objects/*", "objects"] }
        }
      },
      {
        // No s3:DeleteObject. The sync runs with --delete disabled precisely so
        // that a host-side deletion cannot propagate into the backup, and the
        // policy is what makes that a property rather than a flag someone can
        // change. Retention is handled by the lifecycle rules in s3.tf.
        Sid    = "WriteBackupObjects"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:AbortMultipartUpload",
        ]
        Resource = "${aws_s3_bucket.objects.arn}/objects/*"
      },
      {
        Sid    = "EncryptBackupObjects"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
        ]
        Resource = aws_kms_key.main.arn
        Condition = {
          StringEquals = { "kms:ViaService" = "s3.${var.aws_region}.amazonaws.com" }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy" "ecs_instance_telemetry" {
  name = "host-telemetry"
  role = aws_iam_role.ecs_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        // Three groups, not one, and the two extra ones are not a mistake.
        //
        // The `awslogs` log driver is a DOCKER driver: on the EC2 launch type
        // it runs inside the host's Docker daemon, which has no task identity
        // and resolves credentials from IMDS — i.e. from THIS role, not from
        // the task execution role. (Only Fargate, or the opt-in agent flag
        // ECS_ENABLE_AWSLOGS_EXECUTIONROLE_OVERRIDE, routes it through the
        // execution role.) Docker calls CreateLogStream while starting the
        // container, so without api/ and migrate/ here the API task does not
        // log — it fails to start at all, with CannotStartContainerError.
        //
        // The execution role in section 2 keeps its identical grant: it is the
        // one that applies if that agent flag is ever turned on, and a reader
        // comparing the two should see both halves stated rather than infer
        // which one is live.
        Sid    = "ContainerAndHostLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          // The CloudWatch agent looks up a stream before appending to it.
          "logs:DescribeLogStreams",
        ]
        Resource = [
          "${aws_cloudwatch_log_group.host.arn}:*",
          "${aws_cloudwatch_log_group.api.arn}:*",
          "${aws_cloudwatch_log_group.migrate.arn}:*",
        ]
      },
      {
        // PutMetricData accepts no resource, but it does accept a namespace
        // condition — so this grants the ability to write metrics under this
        // deployment's namespace and no other. The metric that matters is disk
        // used_percent on the objects volume: uploads fill it, and a full
        // volume fails ingestion rather than announcing itself.
        Sid      = "HostMetricsInOurNamespaceOnly"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = local.metrics_namespace }
        }
      },
    ]
  })
}

// Session Manager, which is how an operator gets a shell. Written as a managed
// policy attachment rather than by hand: SSM's requirement is ~15 actions
// across ssm/ssmmessages/ec2messages that AWS revises, and a hand-rolled copy
// that drifts fails as a mysterious "instance not managed" rather than as an
// access-denied. There is no inbound SSH rule anywhere in network.tf, so this
// is the only path in.
resource "aws_iam_role_policy_attachment" "ecs_instance_ssm" {
  role       = aws_iam_role.ecs_instance.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

// --- 2. Task execution role ------------------------------------------------
// Used by the ECS agent to *start* a task: pull the image, create the log
// stream, resolve the secrets. It is not the application's identity.

resource "aws_iam_role" "task_execution" {
  name = "${var.project}-task-execution"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        // Stops this role being assumed on behalf of a task in another account
        // that somehow reached the same role ARN.
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "task_execution" {
  name = "start-tasks"
  role = aws_iam_role.task_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "PullApiImageOnly"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
        ]
        // Not the sandbox repository: the sandbox image is pulled by the host's
        // Docker daemon under the instance role, never by a task.
        Resource = aws_ecr_repository.api.arn
      },
      {
        Sid    = "WriteTaskLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = [
          "${aws_cloudwatch_log_group.api.arn}:*",
          "${aws_cloudwatch_log_group.migrate.arn}:*",
        ]
      },
      {
        // Exactly four ARNs, enumerated in secrets.tf. Not a wildcard on
        // "${project}/*": a wildcard would silently grant every secret anyone
        // adds under that prefix later, including ones that have nothing to do
        // with this task.
        Sid      = "ReadNamedSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = local.secret_arns
      },
      {
        Sid      = "DecryptNamedSecrets"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = aws_kms_key.main.arn
        Condition = {
          StringEquals = { "kms:ViaService" = "secretsmanager.${var.aws_region}.amazonaws.com" }
        }
      },
    ]
  })
}

// --- 3. Task roles ---------------------------------------------------------
// These are the application's own identity, and they carry no policies.
//
// That is not an oversight and it is not a stub. The API makes no AWS API calls
// at all: apps/api/pyproject.toml has no boto3 or aioboto3 dependency, objects
// are written to a filesystem path, and the database is reached with a password
// from an environment variable rather than IAM auth. So the correct set of
// permissions is the empty set.
//
// Assigning an empty role is still doing work. With a task role present, ECS
// injects AWS_CONTAINER_CREDENTIALS_RELATIVE_URI and the SDK credential chain
// resolves to *this* role. With no task role at all, the chain falls through to
// IMDS and the container silently inherits the instance profile — which can
// pull images and write to the backup bucket. An empty role shadows the
// instance profile; omitting the role hands it over.

resource "aws_iam_role" "api_task" {
  name        = "${var.project}-api-task"
  description = "API application identity. Intentionally holds no permissions; see iam.tf."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

resource "aws_iam_role" "migrate_task" {
  name        = "${var.project}-migrate-task"
  description = "Alembic migration identity. Intentionally holds no permissions."

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

// --- 4. Data Lifecycle Manager --------------------------------------------
// Snapshots the objects volume. The AWS-managed policy is used here for the
// same reason as SSM's: DLM's required action set is service-internal and a
// hand-rolled copy buys nothing but drift.

resource "aws_iam_role" "dlm" {
  name = "${var.project}-dlm"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "dlm.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "dlm" {
  role       = aws_iam_role.dlm.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

// --- 5. Deploy role (GitHub Actions OIDC, optional) ------------------------
// Push an image, roll the service, run the migration. No long-lived access key
// exists anywhere in this deployment.

resource "aws_iam_openid_connect_provider" "github" {
  count = var.enable_github_oidc && var.github_oidc_provider_arn == "" ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

locals {
  github_provider_arn = var.enable_github_oidc ? (
    var.github_oidc_provider_arn != "" ? var.github_oidc_provider_arn : aws_iam_openid_connect_provider.github[0].arn
  ) : ""
}

resource "aws_iam_role" "deploy" {
  count = var.enable_github_oidc ? 1 : 0

  name = "${var.project}-deploy"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = local.github_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        // Pinned to one repository's default branch. `repo:owner/name:*` would
        // let any pull request from a fork that can trigger the workflow deploy
        // production.
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:${var.github_repo}:ref:refs/heads/main"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "deploy" {
  count = var.enable_github_oidc ? 1 : 0

  name = "deploy"
  role = aws_iam_role.deploy[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "PushImages"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:CompleteLayerUpload",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:DescribeImages",
        ]
        Resource = [
          aws_ecr_repository.api.arn,
          aws_ecr_repository.sandbox.arn,
        ]
      },
      {
        Sid      = "EncryptPushedLayers"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.main.arn
        Condition = {
          StringEquals = { "kms:ViaService" = "ecr.${var.aws_region}.amazonaws.com" }
        }
      },
      {
        Sid    = "RegisterTaskDefinitions"
        Effect = "Allow"
        Action = [
          "ecs:RegisterTaskDefinition",
          "ecs:DescribeTaskDefinition",
        ]
        // Neither action accepts a resource. RegisterTaskDefinition is the
        // dangerous one — it is what iam:PassRole below actually bounds.
        Resource = "*"
      },
      {
        Sid    = "RollTheService"
        Effect = "Allow"
        Action = [
          "ecs:UpdateService",
          "ecs:DescribeServices",
        ]
        Resource = aws_ecs_service.api.id
      },
      {
        // RunTask's resource is the task definition, and the only one this role
        // ever runs is the migration (docs/DEPLOY-AWS.md §7). Naming the family
        // rather than "*" is what stops the deploy role launching an arbitrary
        // task definition — including one somebody else registered for a
        // different purpose — into this cluster.
        Sid      = "RunTheMigration"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:${data.aws_partition.current.partition}:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${aws_ecs_task_definition.migrate.family}:*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.main.arn }
        }
      },
      {
        // Reading task state. These take a task ARN, which does not exist until
        // RunTask returns one, so they are scoped by cluster instead.
        Sid    = "WatchTheMigration"
        Effect = "Allow"
        Action = [
          "ecs:DescribeTasks",
          "ecs:ListTasks",
        ]
        Resource = "*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.main.arn }
        }
      },
      {
        // Bounds RegisterTaskDefinition. Without it, RegisterTaskDefinition +
        // RunTask lets the deployer run a container as any role in the account.
        //
        // What it does NOT do — and the earlier version of this comment claimed
        // otherwise — is make the deploy role harmless. `task_execution` is on
        // this list because a task cannot start without it, and that role reads
        // all four secrets in secrets.tf. A deployer who can register a task
        // definition can therefore register one whose image prints its own
        // environment, and walk away with DATABASE_URL, OPENAI_API_KEY and the
        // Fernet key that protects every stored OAuth token. That is inherent to
        // "CI can deploy this application" and cannot be policed by IAM; it is
        // policed by the trust policy above, which admits exactly one repository
        // on exactly one branch. Treat this role as equivalent to production.
        Sid    = "PassOnlyOurTaskRoles"
        Effect = "Allow"
        Action = "iam:PassRole"
        Resource = [
          aws_iam_role.task_execution.arn,
          aws_iam_role.api_task.arn,
          aws_iam_role.migrate_task.arn,
        ]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
    ]
  })
}
