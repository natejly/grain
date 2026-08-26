locals {
  // The bind mount path, and it is the same string on the host and inside the
  // API container ON PURPOSE. This is the detail that decides whether the
  // sandbox works at all.
  //
  // services/sandbox/container_provider.py builds `-v {root}:/workspace` where
  // root is a directory under SANDBOX_WORKDIR, and hands that to the `docker`
  // CLI. The daemon it reaches is the HOST daemon, so it resolves that bind
  // source against the host filesystem, not against the API container's. If the
  // API container saw the session directory at /data/sandboxes/abc while the
  // host knew it as /var/lib/grain/sandboxes/abc, docker would either fail
  // or silently mount an empty host directory and every execution would run
  // against nothing. Identical paths on both sides is the fix.
  data_mount      = "/var/lib/grain"
  objects_dir     = "${local.data_mount}/objects"
  sandbox_workdir = "${local.data_mount}/sandboxes"

  api_url = "https://${var.api_domain}"

  // Upper-cased Settings field names, verbatim. pydantic-settings maps
  // OBJECTS_DIR -> objects_dir with no alias table, so a typo here is a silently
  // ignored variable rather than a startup error.
  base_environment = {
    // Not merely a label: Settings defaults to "production" and every relaxation
    // in config.py (DEV_AUTO_LOGIN, DEV_USER, MODEL_PROVIDER=scripted,
    // SANDBOX_PROVIDER=subprocess|fake, SESSION_COOKIE_SECURE=false) is refused
    // unless this says development or test.
    APP_ENV  = "production"
    API_HOST = "0.0.0.0"
    API_PORT = "8000"

    // Feeds allowed_web_origins (the CORS allowlist) and primary_web_origin
    // (where email links and OAuth redirects point back at).
    WEB_ORIGIN = var.web_origin

    // Absolute, and that is required rather than tidy. config.py's
    // `project_root()` walks up looking for a directory containing both a
    // Makefile and apps/; inside the API image there is no Makefile, so it falls
    // back to Path.cwd() and a relative OBJECTS_DIR would anchor to whatever the
    // working directory happened to be. Absolute paths bypass the anchoring
    // validator entirely.
    OBJECTS_DIR     = local.objects_dir
    SANDBOX_WORKDIR = local.sandbox_workdir

    MODEL_PROVIDER = "openai"
    OPENAI_MODEL   = var.openai_model

    TOOL_HOST_ALLOWLIST = var.tool_host_allowlist

    // SameSite=None + Secure, and _guard_auth refuses to construct Settings with
    // SESSION_COOKIE_SECURE=false outside development. SESSION_COOKIE_DOMAIN is
    // deliberately unset: the web app is on a different registrable domain, so a
    // Domain attribute cannot help and would only widen the cookie across the
    // API's own subdomains.
    SESSION_COOKIE_SECURE = "true"

    // Where Google sends the browser back, and where the API bounces it
    // afterwards. google_login_callback_url derives from OAUTH_REDIRECT_BASE
    // when GOOGLE_LOGIN_REDIRECT_URI is empty; both are set explicitly because
    // the value has to match the Google console entry character for character.
    OAUTH_REDIRECT_BASE       = local.api_url
    GOOGLE_LOGIN_CLIENT_ID    = var.google_login_client_id
    GOOGLE_LOGIN_REDIRECT_URI = "${local.api_url}/api/auth/google/callback"
    AUTH_POST_LOGIN_REDIRECT  = trimspace(split(",", var.web_origin)[0])

    // The execution feature. `container` is the only value _guard_sandbox
    // permits here: subprocess and fake refuse to construct with
    // APP_ENV=production, and e2b would send workspace documents to a third
    // party (docs/THREAT_MODEL.md).
    SANDBOX_ENABLED                      = var.sandbox_enabled ? "1" : "0"
    SANDBOX_PROVIDER                     = "container"
    SANDBOX_CONTAINER_IMAGE              = aws_ssm_parameter.sandbox_image.value
    SANDBOX_DOCKER_BINARY                = "docker"
    SANDBOX_NETWORK_POLICY               = var.sandbox_network_policy
    SANDBOX_MEMORY_MB                    = tostring(var.sandbox_memory_mb)
    SANDBOX_CPUS                         = tostring(var.sandbox_cpus)
    SANDBOX_MAX_CONCURRENT_PER_WORKSPACE = tostring(var.sandbox_max_concurrent_per_workspace)
  }

  api_environment = [
    for name, value in merge(local.base_environment, var.extra_environment) :
    { name = name, value = value }
  ]

  // valueFrom is a Secrets Manager ARN; the ECS agent resolves it with the
  // execution role at task start and the value never appears in the task
  // definition. Names are the upper-cased Settings fields, same as above.
  api_secrets = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "OPENAI_API_KEY", valueFrom = aws_secretsmanager_secret.openai_api_key.arn },
    { name = "INTEGRATIONS_ENCRYPTION_KEY", valueFrom = aws_secretsmanager_secret.integrations_key.arn },
    { name = "GOOGLE_LOGIN_CLIENT_SECRET", valueFrom = aws_secretsmanager_secret.google_login_client_secret.arn },
  ]
}

resource "aws_ecs_cluster" "main" {
  name = "${var.project}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

// --- API task --------------------------------------------------------------

resource "aws_ecs_task_definition" "api" {
  family = "${var.project}-api"

  // bridge, not awsvpc. The task needs the host's Docker socket regardless, so
  // a task ENI would add per-task security groups around a container that is
  // already root-equivalent on the host — ceremony rather than a boundary. A
  // fixed hostPort also keeps the ALB target group pointed at one stable port.
  network_mode             = "bridge"
  requires_compatibilities = ["EC2"]

  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn      = aws_iam_role.api_task.arn

  volume {
    name      = "docker-socket"
    host_path = "/var/run/docker.sock"
  }

  volume {
    name      = "grain-data"
    host_path = local.data_mount
  }

  container_definitions = jsonencode([
    {
      name  = "api"
      image = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"

      // A hard limit well below the instance's RAM. Sandbox containers are
      // siblings started on the host daemon, so ECS never sees them and never
      // accounts for them: the headroom for
      // SANDBOX_MEMORY_MB x SANDBOX_MAX_CONCURRENT_PER_WORKSPACE has to be left
      // by hand or an execution burst gets the API OOM-killed instead.
      memory = var.api_task_memory_mb
      cpu    = var.api_task_cpu

      essential = true

      portMappings = [{
        containerPort = 8000
        hostPort      = 8000
        protocol      = "tcp"
      }]

      // Root, and this is the cost of the container driver rather than an
      // oversight. /var/run/docker.sock is root:docker on the ECS-optimized AMI
      // and the docker group's gid is not stable across AMI releases, so
      // matching it by number would break on an AMI bump. A process holding
      // this socket can start a privileged container and become root on the
      // host either way — see the preamble in iam.tf for what that does and
      // does not mean here.
      user = "root"

      mountPoints = [
        {
          sourceVolume  = "docker-socket"
          containerPath = "/var/run/docker.sock"
          readOnly      = false
        },
        {
          // Same path inside as outside. See local.data_mount above.
          sourceVolume  = "grain-data"
          containerPath = local.data_mount
          readOnly      = false
        },
      ]

      environment = local.api_environment
      secrets     = local.api_secrets

      // The API shells out to the docker CLI for every execution. Without an
      // init process those exited CLI processes stay as zombies against
      // SANDBOX_PIDS_LIMIT's cousin, the container's own pid table.
      linuxParameters = {
        initProcessEnabled = true
      }

      // Long enough for an in-flight chat turn to finish rather than being cut
      // mid-stream during a deploy.
      stopTimeout = 60

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.api.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "api"
        }
      }
    }
  ])
}

resource "aws_ecs_service" "api" {
  name            = "${var.project}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  launch_type     = "EC2"

  // One, for the reason set out at the top of ec2.tf: background work is
  // in-process and there is no cross-process claiming.
  desired_count = 1

  // 0/100 rather than 100/200, and this is deliberate. A fixed hostPort means
  // two tasks cannot coexist on one instance, so a rolling deploy must stop the
  // old task before starting the new one — roughly 30-60 seconds of downtime
  // per deploy. Dynamic port mapping would remove that, at the price of two API
  // processes running concurrently for those seconds, each with its own
  // in-process executor and its own startup recovery sweep over the same runs
  // table. Brief, honest downtime beats a brief, quiet double-execution.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  // Ingestion and graph rebuild happen at startup recovery; do not let the
  // balancer kill the task while that is running.
  health_check_grace_period_seconds = 90

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  // The image tag is the deployment unit, so a CI push registers a new task
  // definition and this resource must not fight it back to the tag in state.
  lifecycle {
    ignore_changes = [task_definition]
  }

  depends_on = [
    aws_lb_listener.https,
    aws_instance.app,
  ]
}

// --- Migration task --------------------------------------------------------
// `make migrate` is `cd apps/api && alembic upgrade head`. Here it is the same
// command in the same image as the API, run as a one-off task before the
// service rolls. Same image on purpose: a migration is only meaningful against
// the code that ships with it.
//
// No mountPoints and no Docker socket. A migration touches the database and
// nothing else, and giving it the socket would hand host root to a task that
// has no use for it.

resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.project}-migrate"
  network_mode             = "bridge"
  requires_compatibilities = ["EC2"]

  execution_role_arn = aws_iam_role.task_execution.arn
  task_role_arn      = aws_iam_role.migrate_task.arn

  container_definitions = jsonencode([
    {
      name      = "migrate"
      image     = "${aws_ecr_repository.api.repository_url}:${var.api_image_tag}"
      memory    = 512
      cpu       = 256
      essential = true

      // alembic.ini and the versions tree live in apps/api, and env.py takes the
      // URL from Settings rather than from alembic.ini.
      workingDirectory = "/app/apps/api"
      command          = ["alembic", "upgrade", "head"]

      // DATABASE_URL is the only one migrations need. OPENAI_API_KEY is here
      // because Settings refuses to construct without it — `make migrate`
      // already failed this way once in this repo, which is why config.py
      // anchors its env_file to the repo root. extra_environment is merged for
      // the same reason: Settings runs its full production validation here
      // too, so any override needed to make it construct for the API (e.g.
      // EMAIL_SENDER=smtp, observed 2026-08-26) is needed to migrate.
      environment = [
        for name, value in merge({
          APP_ENV        = "production"
          MODEL_PROVIDER = "openai"
          OBJECTS_DIR    = local.objects_dir
          // The comment above predicted this and it still bit: WEB_ORIGIN is
          // needed to CONSTRUCT Settings, not to migrate. A boot guard refuses
          // a production Settings whose origin still resolves to localhost —
          // it would serve credentialed CORS to the visitor's own machine —
          // and that guard runs here too, because `alembic upgrade` imports
          // app.models -> app.database -> get_settings(). Observed 2026-08-26:
          // the migration container exited 1 on that validator before opening
          // a connection, on the first deploy that ever authenticated far
          // enough to run it.
          WEB_ORIGIN = var.web_origin
        }, var.extra_environment) :
        { name = name, value = value }
      ]

      secrets = [
        { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
        { name = "OPENAI_API_KEY", valueFrom = aws_secretsmanager_secret.openai_api_key.arn },
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.migrate.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "migrate"
        }
      }
    }
  ])
}
