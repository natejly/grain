// Two repositories, because the two images have nothing to do with each other.
//
// The API image is the FastAPI application. It must carry a `docker` CLI on its
// PATH, because services/sandbox/container_provider.py shells out to
// SANDBOX_DOCKER_BINARY rather than speaking the Docker API — the container it
// starts is a sibling on the host daemon, not a child.
//
// The sandbox image is infra/sandbox/Dockerfile, which the ADR calls the package
// policy: with SANDBOX_NETWORK_POLICY=none nothing can be installed at runtime,
// so adding a library is a rebuild of this image and a new sandbox_image_tag.
//
// Tags are IMMUTABLE. A deploy is a new tag and a rollback is an older tag; a
// mutable "latest" would make the question "what is actually running" answerable
// only by digest archaeology.

resource "aws_ecr_repository" "api" {
  name                 = "${var.project}/api"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

resource "aws_ecr_repository" "sandbox" {
  name                 = "${var.project}/sandbox"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.main.arn
  }
}

// Untagged layers are build garbage and expire quickly. Tagged images are kept
// deeper than you think you need, because the rollback target is by definition
// an image you did not expect to want.
locals {
  ecr_lifecycle_policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged images after 14 days"
        selection = {
          tagStatus   = "untagged"
          countType   = "sinceImagePushed"
          countUnit   = "days"
          countNumber = 14
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the 20 most recent tagged images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 20
        }
        action = { type = "expire" }
      },
    ]
  })
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy     = local.ecr_lifecycle_policy
}

resource "aws_ecr_lifecycle_policy" "sandbox" {
  repository = aws_ecr_repository.sandbox.name
  policy     = local.ecr_lifecycle_policy
}
