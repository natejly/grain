// Six alarms, chosen because each one corresponds to a failure this topology
// can actually have and a page nobody can ignore. There is no CPU alarm on the
// application host: sandbox executions are supposed to saturate CPU, so a busy
// host is the feature working.

locals {
  metrics_namespace = "${var.project}/host"
}

resource "aws_sns_topic" "alarms" {
  name              = "${var.project}-alarms"
  kms_master_key_id = aws_kms_key.main.id
}

// The default topic policy admits the owning account's IAM principals, which
// CloudWatch is not — it publishes as a service principal. Stated explicitly so
// an alarm that fires is an alarm that arrives.
resource "aws_sns_topic_policy" "alarms" {
  arn = aws_sns_topic.alarms.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudWatchAlarmsToPublish"
      Effect    = "Allow"
      Principal = { Service = "cloudwatch.amazonaws.com" }
      Action    = "SNS:Publish"
      Resource  = aws_sns_topic.alarms.arn
      Condition = {
        StringEquals = { "AWS:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

resource "aws_sns_topic_subscription" "alarm_email" {
  count = var.alarm_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

// The API is down or failing /health. With desired_count = 1 there is no
// degraded state between "serving" and "not serving".
resource "aws_cloudwatch_metric_alarm" "api_unhealthy" {
  alarm_name          = "${var.project}-api-unhealthy"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  threshold           = 1
  period              = 60
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  statistic           = "Maximum"
  alarm_description   = "The single API target is failing GET /health."
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]

  dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }
}

// Sustained 5xx from the application, as opposed to the balancer.
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${var.project}-api-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  threshold           = 10
  period              = 300
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HTTPCode_Target_5XX_Count"
  statistic           = "Sum"
  treat_missing_data  = "notBreaching"
  alarm_description   = "More than 10 application 5xx in 5 minutes, three periods running."
  alarm_actions       = [aws_sns_topic.alarms.arn]

  dimensions = {
    LoadBalancer = aws_lb.api.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "instance_status" {
  alarm_name          = "${var.project}-instance-status"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = 0
  period              = 60
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  alarm_description   = "Instance or system status check failing. Recovery is manual; see docs/DEPLOY-AWS.md."
  alarm_actions       = [aws_sns_topic.alarms.arn]

  dimensions = {
    InstanceId = aws_instance.app.id
  }
}

// The one that bites first in practice. OBJECTS_DIR grows with every upload and
// SANDBOX_WORKDIR grows with every artefact an execution writes; the reaper
// clears sandboxes after SANDBOX_SESSION_IDLE_DAYS but never touches objects.
//
// The dimension set below must be the WHOLE dimension set the CloudWatch agent
// publishes, not a subset — an alarm on three of four dimensions matches no
// metric and sits in INSUFFICIENT_DATA for ever, which is the failure mode
// where you find out the disk alarm never worked on the night it was needed.
// The agent's disk plugin emits path, device and fstype by default, so
// user_data.sh.tftpl sets `"drop_device": true` to remove the one dimension
// that cannot be predicted here (the NVMe name the volume happens to get).
resource "aws_cloudwatch_metric_alarm" "objects_volume_full" {
  alarm_name          = "${var.project}-objects-volume-full"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = 80
  period              = 300
  namespace           = local.metrics_namespace
  metric_name         = "disk_used_percent"
  statistic           = "Maximum"
  alarm_description   = "The objects/sandboxes volume is over 80% full. Ingestion fails when it fills."
  alarm_actions       = [aws_sns_topic.alarms.arn]

  dimensions = {
    InstanceId = aws_instance.app.id
    path       = local.data_mount
    fstype     = "xfs"
  }
}

resource "aws_cloudwatch_metric_alarm" "db_storage" {
  alarm_name          = "${var.project}-db-storage"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  threshold           = 5 * 1024 * 1024 * 1024
  period              = 300
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  statistic           = "Minimum"
  alarm_description   = "Under 5 GiB free on the database. Storage autoscaling is on but is not instant."
  alarm_actions       = [aws_sns_topic.alarms.arn]

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }
}

// t4g classes are burstable. Exhausted CPU credits present as a database that
// is mysteriously slow rather than one that is down, which is far harder to
// diagnose from the application side.
resource "aws_cloudwatch_metric_alarm" "db_cpu_credits" {
  alarm_name          = "${var.project}-db-cpu-credits"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  threshold           = 30
  period              = 300
  namespace           = "AWS/RDS"
  metric_name         = "CPUCreditBalance"
  statistic           = "Minimum"
  treat_missing_data  = "notBreaching"
  alarm_description   = "Burstable database is running out of CPU credits. Move to an m-class instance."
  alarm_actions       = [aws_sns_topic.alarms.arn]

  dimensions = {
    DBInstanceIdentifier = aws_db_instance.main.identifier
  }
}
