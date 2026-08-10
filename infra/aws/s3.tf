// Read this before assuming OBJECTS_DIR points here.
//
// It does not, and cannot today. `objects_dir` is a `Path`, and the two writers
// use it as one: services/ingestion.py:302 does `directory.mkdir(parents=True)`
// and services/analytics.py:229 writes dataset snapshots the same way. There is
// no object-store seam in the codebase — the `S3_ENDPOINT` / `S3_BUCKET` lines
// in .env.example are commented "production adapters" and correspond to no
// field on `Settings`, so setting them configures nothing.
//
// So originals and dataset snapshots live on the encrypted gp3 volume attached
// to the application host (ec2.tf), and this bucket is what makes that
// survivable: a nightly `aws s3 sync` from that volume, plus the versioning and
// lifecycle rules below. docs/RUNBOOK.md's "Backup and retention" section asks
// for the database and the object store to be one logical recovery point; the
// EBS snapshot schedule and the RDS backup window are set to the same hour for
// that reason.
//
// When an object-store backend does land, the bucket, the key, the IAM policy
// and the VPC endpoint are already here and OBJECTS_DIR becomes a URI.

resource "aws_s3_bucket" "objects" {
  bucket = "${var.project}-objects-${data.aws_caller_identity.current.account_id}"

  tags = { Name = "${var.project}-objects" }
}

resource "aws_s3_bucket_public_access_block" "objects" {
  bucket = aws_s3_bucket.objects.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "objects" {
  bucket = aws_s3_bucket.objects.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

// Versioning is the control that makes a sync-based backup a backup: without
// it, a sync that runs after a bad deletion propagates the deletion.
resource "aws_s3_bucket_versioning" "objects" {
  bucket = aws_s3_bucket.objects.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.main.arn
    }
    // One data key per request would be a KMS call per object; a sync of tens of
    // thousands of small originals is exactly the workload this exists for.
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "objects" {
  bucket = aws_s3_bucket.objects.id

  rule {
    id     = "retire-noncurrent"
    status = "Enabled"

    filter {}

    // Long enough to cover the recovery window docs/RUNBOOK.md asks for before
    // tombstoned objects are physically purged.
    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  rule {
    id     = "cool-current"
    status = "Enabled"

    filter {}

    // Originals are read at ingest and rarely again. IA is ~45% cheaper and the
    // retrieval path never touches the bucket.
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
  }

  depends_on = [aws_s3_bucket_versioning.objects]
}

// Three denials that IAM alone cannot express, because a bucket policy binds
// every principal including ones added later: no plaintext transport, and no
// object written under a key other than the deployment's own — the latter split
// across two statements for the reason given on the second of them.
resource "aws_s3_bucket_policy" "objects" {
  bucket = aws_s3_bucket.objects.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.objects.arn,
          "${aws_s3_bucket.objects.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
      {
        // IfExists, and the suffix is the whole point rather than a style tic.
        //
        // A negated string operator is TRUE when the condition key is absent, so
        // a bare StringNotEquals here denies every request that carries no
        // x-amz-server-side-encryption-aws-kms-key-id header at all — and
        // UploadPart is one of those. `aws s3 sync` switches to multipart above
        // multipart_threshold (8 MiB), and the CLI's own transfer manager sends
        // the SSE-KMS arguments only on CreateMultipartUpload: its
        // UPLOAD_PART_ARGS allowlist (awscli/s3transfer/upload.py) contains the
        // SSE-C arguments and nothing else. UploadPart is authorised as
        // s3:PutObject, so the bare form denies it. The nightly backup would
        // copy every small original and 403 on every file over 8 MiB — the
        // uploads most worth having — and nobody would find out until a restore.
        //
        // IfExists denies an upload that names the wrong key while letting a
        // header-less one through, which is safe because the bucket's default
        // encryption above applies this same CMK to exactly those requests.
        Sid       = "DenyUploadsUnderAnotherKey"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.objects.arn}/*"
        Condition = {
          StringNotEqualsIfExists = {
            "s3:x-amz-server-side-encryption-aws-kms-key-id" = aws_kms_key.main.arn
          }
        }
      },
      {
        // The half the statement above cannot carry. Conditions inside one
        // statement are ANDed, so "wrong KMS key OR not KMS at all" needs two.
        // Without this, an uploader that asks for SSE-S3 sends no
        // ...-aws-kms-key-id header, the IfExists above lets it past, and the
        // object lands under an AWS-managed key instead of this deployment's —
        // which is the one property this bucket policy exists to guarantee.
        // Header-less requests still fall through to default encryption.
        Sid       = "DenyUploadsOutsideKms"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.objects.arn}/*"
        Condition = {
          StringNotEqualsIfExists = {
            "s3:x-amz-server-side-encryption" = "aws:kms"
          }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.objects]
}
