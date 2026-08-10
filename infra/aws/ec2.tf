// One instance, declared as an instance rather than as an Auto Scaling group of
// size one. That is a decision, not laziness.
//
// The API tier is singular for a correctness reason, not a cost one. Background
// work runs in-process (`asyncio.to_thread` from the chat, sources, graph and
// tools routers), startup recovery requeues expired leases from
// services/recovery.py, and docs/THREAT_MODEL.md lists "in-process tasks are
// not multi-process production transports" among the known limitations. Two API
// processes would each run their own recovery sweep and their own executor over
// the same `runs` table with only an advisory `run_lease_seconds` between them.
// So desired capacity is 1 until a real queue transport lands.
//
// Given that, an ASG buys auto-replacement — of a host whose state is a
// specific EBS volume that has to be reattached. That is not automatic, so the
// ASG would be ceremony around a manual procedure. A named instance and a named
// volume make the recovery procedure honest and short (docs/DEPLOY-AWS.md).

data "aws_ssm_parameter" "ecs_ami" {
  name = var.ecs_ami_ssm_parameter
}

// Not baked into user_data: the instance is long-lived and the sandbox image is
// not, so rolling the image must not mean replacing the host.
resource "aws_ssm_parameter" "sandbox_image" {
  name        = "/${var.project}/sandbox-image"
  description = "Image URI the host Docker daemon pulls for sandbox execution"
  type        = "String"
  value       = "${aws_ecr_repository.sandbox.repository_url}:${var.sandbox_image_tag}"
}

resource "aws_iam_role_policy" "ecs_instance_sandbox_param" {
  name = "sandbox-image-parameter"
  role = aws_iam_role.ecs_instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "ssm:GetParameter"
      Resource = aws_ssm_parameter.sandbox_image.arn
    }]
  })
}

// Separate from the root volume so replacing the instance does not destroy
// uploaded originals and dataset snapshots. It carries OBJECTS_DIR and
// SANDBOX_WORKDIR; the root volume carries only the OS and pulled images.
resource "aws_ebs_volume" "data" {
  availability_zone = aws_subnet.public[0].availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true
  kms_key_id        = aws_kms_key.main.arn

  tags = {
    Name   = "${var.project}-data"
    Backup = "daily"
  }

  lifecycle {
    // The object store. `terraform apply` must never be able to delete it as a
    // side effect of an unrelated change.
    prevent_destroy = true
  }
}

resource "aws_instance" "app" {
  ami                    = data.aws_ssm_parameter.ecs_ami.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.app.id]
  iam_instance_profile   = aws_iam_instance_profile.ecs_instance.name

  // A public IP, and network.tf explains why: this is what replaces a NAT
  // gateway. Inbound is one port from the ALB security group and nothing else.
  associate_public_ip_address = true

  // IMDSv2 required. The API container reaches IMDS in bridge networking, so
  // token-requiring IMDS is what stops an SSRF-shaped bug in the API from
  // reading the instance profile with a bare GET.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 2
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    volume_size           = var.root_volume_gb
    volume_type           = "gp3"
    encrypted             = true
    kms_key_id            = aws_kms_key.main.arn
    delete_on_termination = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region              = var.aws_region
    cluster_name        = aws_ecs_cluster.main.name
    data_mount          = local.data_mount
    data_volume_id      = aws_ebs_volume.data.id
    sandbox_image_param = aws_ssm_parameter.sandbox_image.name
    objects_bucket      = aws_s3_bucket.objects.bucket
    kms_key_arn         = aws_kms_key.main.arn
    host_log_group      = aws_cloudwatch_log_group.host.name
    metrics_namespace   = local.metrics_namespace
  })

  // Editing the boot script must not silently destroy the host. Apply the
  // change, then re-run it through SSM (docs/DEPLOY-AWS.md) or reboot.
  user_data_replace_on_change = false

  // No key_name. There is no inbound SSH rule and no key pair anywhere in this
  // configuration; shell access is Session Manager, audited in CloudTrail.

  tags = { Name = "${var.project}-app" }

  depends_on = [aws_iam_role_policy.ecs_instance_agent]
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.app.id

  // On a Nitro instance the guest sees an NVMe device whose serial is the
  // volume id; user_data.sh.tftpl matches on that rather than on this name.
}

// The other half of the recovery point: RDS keeps automated backups in its own
// window, S3 versioning keeps the synced objects, and this keeps a
// crash-consistent image of the volume itself.
resource "aws_dlm_lifecycle_policy" "data" {
  description        = "${var.project} objects volume snapshots"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]
    target_tags = {
      Backup = "daily"
    }

    schedule {
      name = "daily-7"

      create_rule {
        // Same hour as the RDS backup window and the object sync.
        cron_expression = "cron(15 7 ? * * *)"
      }

      retain_rule {
        count = 7
      }

      copy_tags = true
    }
  }
}
