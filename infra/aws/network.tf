// The whole point of this file is what it does NOT contain: there is no NAT
// gateway and no egress-only gateway anywhere in this VPC.
//
// The reason is SANDBOX_NETWORK_POLICY=none. Generated code runs with
// `docker run --network none`, so the execution path has no route to filter and
// nothing to pay for. What is left needing egress is the API process itself —
// api.openai.com, ECR, Secrets Manager, CloudWatch Logs, S3 — and a private
// subnet cannot reach api.openai.com without a NAT gateway ($0.045/hr plus
// $0.045/GB, ~$33/month/AZ before traffic) or the API cannot boot at all, since
// Settings refuses to construct without a usable OPENAI_API_KEY.
//
// So the application host sits in a public subnet with a public IP and a
// security group that accepts nothing except the ALB. Inbound surface is one
// port from one source; the public IP buys the egress a NAT gateway would
// otherwise have sold us. Shell access is Session Manager (outbound only, no
// inbound rule, no key pair). The database sits in subnets with no route to the
// internet gateway at all.

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = "${var.project}-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${var.project}-igw" }
}

// Two public subnets because an ALB requires two AZs, not because the API is
// spread across them. Exactly one of them ever holds an instance.
resource "aws_subnet" "public" {
  count = 2

  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true

  tags = { Name = "${var.project}-public-${count.index}" }
}

// Two private subnets because an RDS subnet group requires two AZs. They have
// no default route; nothing in them can reach or be reached from the internet.
resource "aws_subnet" "private" {
  count = 2

  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 8)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "${var.project}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "${var.project}-public" }
}

// Deliberately routeless apart from the VPC-local route AWS creates implicitly
// and the S3 gateway endpoint association below. Adding a default route here is
// what a NAT gateway would be for; do not add one without deciding to pay for it.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  tags = { Name = "${var.project}-private" }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count = 2

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

// A gateway endpoint, which unlike an interface endpoint costs nothing per hour
// and nothing per GB. The nightly object-store sync therefore leaves the VPC
// over AWS's own path rather than over the internet gateway. Interface
// endpoints for ECR/Logs/Secrets Manager were considered and rejected: at
// ~$7.30/month each they would add ~$29/month to remove traffic from a route
// the host needs open anyway for api.openai.com.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id, aws_route_table.private.id]

  tags = { Name = "${var.project}-s3" }
}

// --- Security groups -------------------------------------------------------
// Three groups, each referencing the next by id rather than by CIDR, so the
// rules stay true if an address ever changes.

resource "aws_security_group" "alb" {
  name        = "${var.project}-alb"
  description = "Public TLS entry point"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project}-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "TLS from the internet"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_ingress_rule" "alb_https_v6" {
  security_group_id = aws_security_group.alb.id
  description       = "TLS from the internet (IPv6)"
  cidr_ipv6         = "::/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

// Port 80 exists only to answer with a 301. It never carries a session cookie:
// the cookie is Secure, so a browser will not send it over http regardless.
resource "aws_vpc_security_group_ingress_rule" "alb_http_redirect" {
  security_group_id = aws_security_group.alb.id
  description       = "Plain HTTP, redirected to HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 80
  to_port           = 80
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "alb_to_api" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Forward to the API container port only"
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}

resource "aws_security_group" "app" {
  name        = "${var.project}-app"
  description = "API and sandbox host"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project}-app" }
}

// The only inbound rule on the host. No SSH: shell access is SSM Session
// Manager, which is an outbound connection from the instance and therefore
// needs no ingress at all.
resource "aws_vpc_security_group_ingress_rule" "app_from_alb" {
  security_group_id            = aws_security_group.app.id
  description                  = "API port, from the load balancer only"
  referenced_security_group_id = aws_security_group.alb.id
  from_port                    = 8000
  to_port                      = 8000
  ip_protocol                  = "tcp"
}

// Outbound 443 covers api.openai.com, ECR, Secrets Manager, CloudWatch Logs,
// SSM and S3. It does not cover the sandbox, which has no interface: sibling
// containers are started with --network none and never join this group.
resource "aws_vpc_security_group_egress_rule" "app_https" {
  security_group_id = aws_security_group.app.id
  description       = "Model provider, AWS control planes, package-free image pulls"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
}

resource "aws_vpc_security_group_egress_rule" "app_to_db" {
  security_group_id            = aws_security_group.app.id
  description                  = "PostgreSQL"
  referenced_security_group_id = aws_security_group.db.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

// Outbound SMTP submission, only when EMAIL_SENDER=smtp is actually configured.
//
// This is not optional decoration: `send_quietly` swallows mail *errors*, but a
// blocked SMTP port is not an error — the SYN is blackholed and smtplib sits on
// its 15s timeout for connect, EHLO and login in turn. Signup sends the
// verification mail inside the request, so with egress closed every account
// creation took **45 seconds** and the UI sat on "Working…" until users gave
// up. Observed in production 2026-08-26.
locals {
  // Derived from extra_environment so the hole opens exactly when the
  // deployment is configured to send mail, and closes again if it is not.
  smtp_port = lookup(var.extra_environment, "EMAIL_SENDER", "") == "smtp" ? tonumber(lookup(var.extra_environment, "SMTP_PORT", "587")) : null
}

resource "aws_vpc_security_group_egress_rule" "app_smtp" {
  count = local.smtp_port == null ? 0 : 1

  security_group_id = aws_security_group.app.id
  description       = "SMTP submission for transactional mail (EMAIL_SENDER=smtp)"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = local.smtp_port
  to_port           = local.smtp_port
  ip_protocol       = "tcp"
}

// No port 53 rule: Amazon DNS is answered by the VPC resolver at the VPC base
// +2 address, which security groups do not filter. No port 80 rule either —
// nothing this host talks to is plaintext.

resource "aws_security_group" "db" {
  name        = "${var.project}-db"
  description = "RDS PostgreSQL"
  vpc_id      = aws_vpc.main.id

  tags = { Name = "${var.project}-db" }
}

resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from the application host only"
  referenced_security_group_id = aws_security_group.app.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
}

// No egress rules on the database group at all. RDS does not originate
// connections in this deployment, and the default-deny that results from
// declaring zero egress rules is the point.
