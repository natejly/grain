# infra/aws

Terraform for the AWS deployment. **`docs/DEPLOY-AWS.md` is the document** —
the decision, the topology, the cost model and the runbook. This file is only a
map of what is here.

```
versions.tf     provider pins and the (commented) S3 state backend
variables.tf    every input, each with the reason it exists
network.tf      VPC, subnets, routes, security groups — and no NAT gateway
kms.tf          one CMK for RDS, EBS, S3, ECR and Secrets Manager
ecr.tf          two immutable-tag repositories: the API and the sandbox image
rds.tf          PostgreSQL 16, private, force_ssl, and the DATABASE_URL local
s3.tf           the objects BACKUP bucket (not the object store; see the header)
secrets.tf      four Secrets Manager entries mapped to Settings fields
iam.tf          four roles, written out longhand, including the empty one
alb.tf          public TLS entry point
ec2.tf          the single host, its persistent data volume, snapshot policy
ecs.tf          cluster, API task + service, one-off migration task
monitoring.tf   SNS topic and six alarms
logs.tf         three CloudWatch log groups
outputs.tf      what you need for DNS, deploys and shells
user_data.sh.tftpl   host bootstrap: ECS join, data volume, sandbox image pull, backup timer
terraform.tfvars.example
```

Three things a reader should know before changing anything:

1. **There is no NAT gateway on purpose.** `SANDBOX_NETWORK_POLICY=none` means
   the execution path has no route to filter, and the only process needing
   egress is the API. See the header comment in `network.tf`.
2. **`local.data_mount` is the same string on the host and inside the API
   container on purpose.** The sandbox driver hands bind-mount paths to the host
   Docker daemon. See the header comment in `ecs.tf`.
3. **`desired_count = 1` is a correctness constraint**, not a cost one.
   Background work is in-process. See the header comment in `ec2.tf`.

Validated with OpenTofu 1.12.5 (`fmt`, `init -backend=false`, `validate`).
Never applied — no AWS account was available. See `docs/DEPLOY-AWS.md` §11–12.
