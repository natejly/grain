// PostgreSQL, because docs/ARCHITECTURE.md names it the production system of
// record and docs/THREAT_MODEL.md lists SQLite among the known MVP limitations:
// "SQLite and in-process tasks are not multi-process production transports."
// The application reaches it through DATABASE_URL and nothing else — engine
// construction in app/database.py branches on the "sqlite" prefix and otherwise
// just hands the URL to SQLAlchemy with pool_pre_ping.
//
// Vanilla PostgreSQL is enough. There is no pgvector requirement despite what
// infra/compose.yaml's image name suggests: chunk and memory embeddings are
// `LargeBinary` columns (app/models.py) scored in Python, bounded by
// RETRIEVAL_VECTOR_CANDIDATE_CAP and MEMORY_RECALL_CANDIDATE_CAP. That is why
// the API host's RAM matters more than the database's.

locals {
  // The classes RDS excludes from Performance Insights. Enumerated rather than
  // pattern-matched on "t": db.t3.medium and db.t4g.medium DO support it, so a
  // regex on the family would silently disable a feature that is available.
  performance_insights_unsupported = [
    "db.t2.micro",
    "db.t2.small",
    "db.t3.micro",
    "db.t3.small",
    "db.t4g.micro",
    "db.t4g.small",
  ]
  performance_insights_supported = !contains(
    local.performance_insights_unsupported, var.db_instance_class
  )
}

resource "random_password" "db" {
  length  = 40
  special = true
  // The password is embedded in a URL that SQLAlchemy parses, so restrict the
  // alphabet to characters that survive a URL without percent-encoding. A
  // generated '@' or '/' here produces a connection failure that looks like a
  // wrong host.
  override_special = "-_.~"
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-db"
  subnet_ids = aws_subnet.private[*].id

  tags = { Name = "${var.project}-db" }
}

// rds.force_ssl rejects any unencrypted connection at the server, so the
// `sslmode=require` in DATABASE_URL is enforced rather than requested. The
// database is already unreachable from outside the VPC; this closes the case
// where something inside it connects in plaintext.
resource "aws_db_parameter_group" "main" {
  name_prefix = "${var.project}-pg${var.db_engine_version}-"
  family      = "postgres${var.db_engine_version}"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  // Log statements slower than a second. Retrieval and analytics do their own
  // bounding, so anything above this is a query nobody intended to write.
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_db_instance" "main" {
  identifier     = "${var.project}-db"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_allocated_storage * 4
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.main.arn

  db_name  = replace(var.project, "-", "_")
  username = replace(var.project, "-", "_")
  // Not manage_master_user_password: the application consumes a single
  // DATABASE_URL string, so an RDS-managed secret holding {username, password}
  // JSON would need an entrypoint to assemble the URL, and that entrypoint
  // lives in an image this configuration does not own. Generating the password
  // here keeps the deployment code-free at the cost of putting the password in
  // Terraform state — which is why versions.tf insists on an encrypted backend.
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period = var.db_backup_retention_days
  backup_window           = "07:00-08:00"
  maintenance_window      = "Mon:08:30-Mon:09:30"
  copy_tags_to_snapshot   = true

  auto_minor_version_upgrade = true
  deletion_protection        = var.deletion_protection
  skip_final_snapshot        = false
  final_snapshot_identifier  = "${var.project}-db-final"

  // Derived from the instance class rather than hardcoded true, because RDS
  // does not offer Performance Insights on the smallest burstable classes and
  // rejects CreateDBInstance outright when you ask for it there — and
  // db_instance_class DEFAULTS to db.t4g.small, so `true` here fails the very
  // first apply. Deriving it means bumping db_instance_class turns Performance
  // Insights back on without a second edit and without anyone remembering.
  performance_insights_enabled          = local.performance_insights_supported
  performance_insights_retention_period = local.performance_insights_supported ? 7 : null
  performance_insights_kms_key_id       = local.performance_insights_supported ? aws_kms_key.main.arn : null
  enabled_cloudwatch_logs_exports       = ["postgresql"]

  tags = { Name = "${var.project}-db" }
}

locals {
  // The exact string DATABASE_URL takes. `postgresql+psycopg` is psycopg 3,
  // which is what apps/api/pyproject.toml's `postgres` extra installs; the
  // API image must be built with that extra or SQLAlchemy will fail to find a
  // driver at import time.
  database_url = format(
    "postgresql+psycopg://%s:%s@%s:%d/%s?sslmode=require",
    aws_db_instance.main.username,
    random_password.db.result,
    aws_db_instance.main.address,
    aws_db_instance.main.port,
    aws_db_instance.main.db_name,
  )
}
