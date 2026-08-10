// An ALB rather than an Elastic IP with a TLS terminator on the box. The
// session cookie is Secure + SameSite=None (app/services/auth/sessions.py), so
// HTTPS is not optional and a certificate that expires is a total outage. ACM
// renews itself; certbot on a host that is also the sandbox host is a cron job
// and a failure mode. The runner-up costs ~$22/month less and is named in
// docs/DEPLOY-AWS.md.

resource "aws_lb" "api" {
  name               = "${var.project}-api"
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  // Rejects requests carrying malformed headers instead of normalising them.
  // The API's own request-id middleware validates X-Request-ID against a
  // pattern, but the CSRF header (X-CSRF-Token) is compared as bytes and a
  // header-smuggling primitive in front of that check is worth removing.
  drop_invalid_header_fields = true

  // Longer than OPENAI_TIMEOUT_SECONDS (60) so a slow model turn is attributed
  // to the model rather than appearing as a 504 from the balancer. Chat streams
  // are Server-Sent Events; this is the ceiling on a quiet stream.
  idle_timeout = 120

  enable_deletion_protection = var.deletion_protection

  tags = { Name = "${var.project}-api" }
}

resource "aws_lb_target_group" "api" {
  name        = "${var.project}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "instance"
  vpc_id      = aws_vpc.main.id

  // GET /health checks API and database readiness (docs/RUNBOOK.md). It is the
  // only unauthenticated endpoint suitable for this.
  health_check {
    path                = "/health"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  // Nothing in the API is instance-affine — sessions are database rows, not
  // memory — so no stickiness. Deregistration is quick because there is only
  // ever one target and a deploy has already stopped it.
  deregistration_delay = 15

  tags = { Name = "${var.project}-api" }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  // TLS 1.2 floor with 1.3 preferred. A browser that cannot negotiate this
  // cannot store a SameSite=None cookie correctly either.
  ssl_policy      = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"

  // 301, not a forward. There is no plaintext path to the API: the browser must
  // be on https before it will attach the Secure session cookie at all, so
  // serving anything here would only produce confusing anonymous requests.
  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}
