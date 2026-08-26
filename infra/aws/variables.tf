variable "aws_region" {
  description = "Region for every resource in this configuration."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Name prefix for every resource. Short, lowercase, DNS-safe."
  type        = string
  default     = "grain"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.project))
    error_message = "project must be 3-21 chars, lowercase alphanumeric or hyphen, starting with a letter."
  }
}

variable "vpc_cidr" {
  description = "CIDR for the VPC. /16 leaves room for the four /20 subnets below."
  type        = string
  default     = "10.42.0.0/16"
}

variable "api_domain" {
  description = <<-EOT
    Public hostname the API answers on, e.g. api.example.com. This becomes
    OAUTH_REDIRECT_BASE and the base of GOOGLE_LOGIN_REDIRECT_URI, so it must
    match the Google console entry exactly and must be covered by
    acm_certificate_arn.
  EOT
  type        = string
}

variable "web_origin" {
  description = <<-EOT
    Comma-separated origins allowed to send credentialed requests, e.g.
    "https://app.example.com". Becomes WEB_ORIGIN, which feeds both the CORS
    allowlist and AUTH_POST_LOGIN_REDIRECT. Must be https: the session cookie is
    SameSite=None, which browsers only honour alongside Secure.
  EOT
  type        = string

  validation {
    condition     = alltrue([for o in split(",", var.web_origin) : startswith(trimspace(o), "https://")])
    error_message = "every web_origin entry must be https:// — SameSite=None cookies require Secure."
  }
}

variable "acm_certificate_arn" {
  description = "ACM certificate in aws_region covering api_domain. Terminates TLS at the ALB."
  type        = string
}

variable "instance_type" {
  description = <<-EOT
    The single API + sandbox host. Sized for the API process plus concurrent
    sibling sandbox containers at SANDBOX_MEMORY_MB x SANDBOX_MAX_CONCURRENT_PER_WORKSPACE.
    m7g.large (2 vCPU / 8 GiB, arm64) fits the API plus two 2 GiB sandboxes.
    Both container images must be built for this instance's architecture.
  EOT
  type        = string
  default     = "m7g.large"
}

variable "ecs_ami_ssm_parameter" {
  description = <<-EOT
    SSM public parameter naming the ECS-optimized AMI. Must match
    instance_type's architecture: .../arm64/recommended/image_id for Graviton,
    .../x86_64/recommended/image_id for Intel.
  EOT
  type        = string
  default     = "/aws/service/ecs/optimized-ami/amazon-linux-2023/arm64/recommended/image_id"
}

variable "data_volume_gb" {
  description = <<-EOT
    Persistent gp3 volume mounted at /var/lib/grain, holding OBJECTS_DIR and
    SANDBOX_WORKDIR. Separate from the root volume so replacing the instance
    does not destroy uploaded originals or dataset snapshots.
  EOT
  type        = number
  default     = 100
}

variable "root_volume_gb" {
  description = "Root volume. Holds the OS, the ECS agent, and the pulled container images."
  type        = number
  default     = 60
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.small is 2 vCPU / 2 GiB."
  type        = string
  default     = "db.t4g.small"
}

variable "db_allocated_storage" {
  description = "RDS gp3 storage in GiB."
  type        = number
  default     = 50
}

variable "db_engine_version" {
  description = "PostgreSQL major version. The app uses no extensions; embeddings are LargeBinary."
  type        = string
  default     = "16"
}

variable "db_multi_az" {
  description = <<-EOT
    Multi-AZ RDS roughly doubles the database line item. The API tier is a
    single instance by design (see docs/DEPLOY-AWS.md), so a Multi-AZ database
    removes one failure mode out of two rather than delivering availability.
  EOT
  type        = bool
  default     = false
}

variable "db_backup_retention_days" {
  description = "RDS automated backup retention. Also sets the PITR window."
  type        = number
  default     = 7
}

variable "deletion_protection" {
  description = "Deletion protection on the ALB and RDS. Leave true for anything real."
  type        = bool
  default     = true
}

variable "api_image_tag" {
  description = <<-EOT
    Immutable tag in the API ECR repository. ECR repositories here are
    IMMUTABLE, so this is never "latest": a redeploy is a new tag, which is what
    makes a rollback a tag change rather than an archaeology exercise.
  EOT
  type        = string
}

variable "sandbox_image_tag" {
  description = "Immutable tag in the sandbox ECR repository (infra/sandbox/Dockerfile)."
  type        = string
}

variable "api_task_memory_mb" {
  description = <<-EOT
    Hard memory limit for the API container. Deliberately well under the
    instance's RAM: sandbox containers are siblings spawned on the host daemon,
    so ECS does not account for them and the headroom has to be left by hand.
  EOT
  type        = number
  default     = 3072
}

variable "api_task_cpu" {
  description = "CPU shares for the API container (1024 = one vCPU). Leaves room for sandbox siblings."
  type        = number
  default     = 768
}

variable "openai_model" {
  description = "OPENAI_MODEL. The app has no offline mode; OPENAI_API_KEY is mandatory."
  type        = string
  default     = "gpt-5.5"
}

variable "tool_host_allowlist" {
  description = "TOOL_HOST_ALLOWLIST. Exact hosts, comma separated. Empty denies all tool fetches."
  type        = string
  default     = "api.github.com"
}

variable "sandbox_enabled" {
  description = "SANDBOX_ENABLED. Off means no execution tools reach the model at all."
  type        = bool
  default     = true
}

variable "sandbox_network_policy" {
  description = <<-EOT
    SANDBOX_NETWORK_POLICY. Leave it at "none": that is the reason this topology
    needs no NAT gateway and has no exfiltration path.

    "open" is accepted but does not do what its name says. The container driver
    hardcodes `--network none` in `_docker_argv`
    (services/sandbox/container_provider.py) and never reads the policy when
    building the run, so "open" changes exactly two things: `NO_NETWORK=1`
    disappears from the sandbox environment, and services/sandbox/tools.py tells
    the model "it can reach the whole internet" — which is then false, and every
    request the model makes on the strength of it fails. The failure is safe but
    confusing. Until the driver honours the policy, "none" is the only value
    with a truthful meaning here. "allowlist" is refused by the driver outright
    rather than silently downgraded, which is why the validation below rejects it.
  EOT
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "open"], var.sandbox_network_policy)
    error_message = "the container driver supports only 'none' or 'open'; it raises on 'allowlist'."
  }
}

variable "sandbox_memory_mb" {
  description = "SANDBOX_MEMORY_MB, per execution. Multiply by concurrency when sizing instance_type."
  type        = number
  default     = 2048
}

variable "sandbox_cpus" {
  description = "SANDBOX_CPUS, per execution."
  type        = number
  default     = 2.0
}

variable "sandbox_max_concurrent_per_workspace" {
  description = <<-EOT
    SANDBOX_MAX_CONCURRENT_PER_WORKSPACE. On m7g.large, 3 x 2 GiB sandboxes plus
    a 3 GiB API container oversubscribes 8 GiB, so this defaults to 2 here even
    though the application default is 3.
  EOT
  type        = number
  default     = 2
}

variable "google_login_client_id" {
  description = <<-EOT
    GOOGLE_LOGIN_CLIENT_ID. Not a secret (it is public in the OAuth redirect),
    so it lives in the task definition rather than Secrets Manager. This is the
    sign-in client, deliberately separate from the Gmail data-integration
    client — sharing one would let a mailbox grant double as proof of identity.
    Empty disables federated sign-in; password login still works.
  EOT
  type        = string
  default     = ""
}

variable "extra_environment" {
  description = <<-EOT
    Additional non-secret environment variables merged into the API task
    definition, keyed by the upper-cased Settings field name (SMTP_HOST,
    EMAIL_FROM, RETRIEVAL_CONTEXTUAL, ...). An escape hatch so tuning a setting
    does not mean editing ecs.tf. Anything sensitive belongs in secrets.tf
    instead: task-definition environment is readable by any principal with
    ecs:DescribeTaskDefinition.
  EOT
  type        = map(string)
  default     = {}

  // This map is merged OVER local.base_environment, so without this an
  // `extra_environment = { APP_ENV = "development" }` would win — and APP_ENV
  // is the single key every safety gate in apps/api/app/config.py hangs off.
  // Flip it and DEV_AUTO_LOGIN, DEV_USER, MODEL_PROVIDER=scripted,
  // SANDBOX_PROVIDER=subprocess and SESSION_COOKIE_SECURE=false all become
  // settable from this same map, because each of those validators only refuses
  // outside development/test. An escape hatch for tuning must not also be an
  // escape hatch for the gates.
  validation {
    condition = length(setintersection(keys(var.extra_environment), [
      "APP_ENV",
      "MODEL_PROVIDER",
      "SANDBOX_PROVIDER",
      "DEV_AUTO_LOGIN",
      "DEV_USER",
      "SESSION_COOKIE_SECURE",
    ])) == 0
    error_message = "extra_environment must not set APP_ENV, MODEL_PROVIDER, SANDBOX_PROVIDER, DEV_AUTO_LOGIN, DEV_USER or SESSION_COOKIE_SECURE — those are the gates config.py enforces, not tuning knobs."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the API and migration log groups."
  type        = number
  default     = 30
}

variable "alarm_email" {
  description = "Optional address subscribed to the alarm topic. Empty creates the topic with no subscription."
  type        = string
  default     = ""
}

variable "enable_github_oidc" {
  description = "Create a GitHub Actions OIDC deploy role. Requires github_repo."
  type        = bool
  default     = false
}

variable "github_repo" {
  description = "owner/name of the repository allowed to assume the deploy role."
  type        = string
  default     = ""
}

variable "github_environment" {
  description = <<-EOT
    Name of the GitHub environment the deploy job runs in ("uat", "production").
    A job that names an environment gets `environment:<name>` in its OIDC
    subject in place of the branch ref, so the deploy role's trust policy has
    to name it or the role cannot be assumed. Empty allows only the ref form.
  EOT
  type        = string
  default     = ""
}

variable "github_oidc_provider_arn" {
  description = <<-EOT
    ARN of an existing GitHub OIDC provider in this account. Empty and
    enable_github_oidc=true creates one; an account that already has a provider
    for token.actions.githubusercontent.com must pass it here, because the
    provider is a per-account singleton.
  EOT
  type        = string
  default     = ""
}
