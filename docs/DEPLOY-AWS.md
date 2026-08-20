# Deploying Grain on AWS

Terraform for everything here lives in `infra/aws/`. This document is the
decision, the topology and the runbook; the Terraform is the authority on the
details.

---

## 1. The decision

**The API runs as an ECS task on a single EC2 instance, and that instance's
Docker daemon is what runs the sandbox.**

One constraint decides the whole shape of this deployment. `SANDBOX_PROVIDER=container`
is the only driver `_guard_sandbox` permits with `APP_ENV=production` that keeps
user documents inside the deployment's own infrastructure, and
`services/sandbox/container_provider.py` implements it by shelling out to
`docker run --rm`. That needs a Docker socket the API process can reach.
**Fargate cannot spawn containers from a task.** So the API has to live
somewhere with a daemon, and there were three honest ways to give it one.

| Option | Verdict |
|---|---|
| **ECS on EC2** | **Chosen.** |
| Plain EC2 host, API under systemd | Rejected. |
| Fargate API + separate EC2 executor service | Rejected. |

### Why ECS-on-EC2 over a plain EC2 host

The two cost the same — the ECS control plane is free, and both are one
`m7g.large`. So this is not a money decision, and if it were, plain EC2 would
win by a rounding error. It is a decision about three things ECS gives you for
that same zero dollars:

1. **A task role separate from the instance profile.** On a plain host the API
   process runs under the instance profile, so it inherits whatever the host
   needs (ECR pull, S3 backup writes, SSM). Under ECS the API gets its own role,
   which in `infra/aws/iam.tf` has *no policies at all* — the application has no
   `boto3` dependency and makes no AWS calls. That empty role is a real control:
   with a task role assigned, the SDK credential chain resolves to it; with none,
   the container falls through to IMDS and silently picks up the instance
   profile.
2. **`make migrate` becomes `aws ecs run-task`.** Same image, same secrets,
   different command, and its own IAM role with no Docker socket. On a plain
   host the migration is an SSH-shaped ritual.
3. **A deploy is a task-definition revision.** Image pull, log driver, secret
   resolution and health-checked rollout are already written. On a plain host
   you write them.

### Why not Fargate + an executor service

This is the option that sounds most correct and is the most expensive, in both
senses.

- In dollars: a 1 vCPU / 2 GiB Fargate task is about **$36/month on top of** the
  EC2 executor, which you still need. Nothing is removed.
- In code: `container_provider.py` shells out to a local `docker` binary. There
  is no HTTP executor driver in this repository. A separate executor service
  means writing a new `SandboxProvider`, an authenticated exec transport, and a
  new trust boundary — for a component whose job is to be the thing that runs
  untrusted code. That is a design worth doing at a scale this deployment does
  not have.

### And why not E2B

`SANDBOX_PROVIDER=e2b` removes the sizing pressure entirely — the host could
drop to an `m7g.medium` and save ~$30/month. `docs/THREAT_MODEL.md` states the
cost plainly: *"uploaded workspace documents leave the deployment's
infrastructure and are processed by a third party under their terms."* The seam
stays; flipping to it is `SANDBOX_PROVIDER` plus `SANDBOX_API_KEY`. It is not
the default.

### The other thing that is singular, and why

`desired_count = 1` is not thrift. Background work in this codebase runs
in-process (`asyncio.to_thread` from the chat, sources, graph and tools
routers), startup recovery re-queues expired leases from
`services/recovery.py`, and `docs/THREAT_MODEL.md` lists *"in-process tasks are
not multi-process production transports"* among the known limitations. Two API
processes would each run their own recovery sweep and their own executor over
the same `runs` table with nothing between them but an advisory
`RUN_LEASE_SECONDS`. So: one instance, one task, **one uvicorn worker**, until a
real queue transport lands. Horizontal scale is a code change, not a Terraform
change, and pretending otherwise in the infrastructure would be a lie that
corrupts data.

The honest consequence is stated in §9: this topology has a single-AZ,
single-host failure mode with a manual recovery procedure.

---

## 2. Topology

```
                    Vercel (Next.js)                Browser
                    app.example.com  <───────────────┐
                                                     │  fetch(credentials: "include")
                                                     │  Cookie: fieldnote_session
                                                     ▼
                                          ACM  ┌─────────────────┐
                                        TLS 1.2+│  ALB :443/:80  │  public subnets (2 AZ)
                                               └────────┬────────┘
                                                        │ HTTP :8000, alb-sg only
   VPC 10.42.0.0/16 ────────────────────────────────────┼──────────────────────────────
                                                        ▼
     public subnet A                         ┌──────────────────────────┐
     ┌─────────────────────────────────┐     │  m7g.large, public IP    │
     │ ECS task "api" (bridge, :8000)  │     │  ECS agent + Docker      │
     │   /var/run/docker.sock  ────────┼─────┼──► host daemon           │
     │   /var/lib/grain    ────────┼─────┼──► gp3 100 GB (encrypted)│
     │   root, task role: NO POLICIES  │     │                          │
     └──────────────┬──────────────────┘     │  sibling containers:     │
                    │ docker run --rm        │  --network none          │
                    │                        │  --read-only --cap-drop  │
                    │                        │  ALL --user 65534        │
                    ▼                        └──────────────────────────┘
        ┌───────────────────────┐                       │
        │  sandbox execution    │  NO NETWORK INTERFACE │ :443 out via IGW
        │  (no route anywhere)  │                       │ (api.openai.com, ECR,
        └───────────────────────┘                       │  Secrets Manager, Logs)
                    │ :5432                             ▼
     private subnets (no route to IGW, NO NAT GATEWAY)
     ┌────────────────────────────┐        ┌─────────────────────────┐
     │ RDS PostgreSQL 16          │        │ S3 gateway endpoint     │
     │ db.t4g.small, encrypted    │        │ (free) ──► objects      │
     │ rds.force_ssl = 1          │        │           backup bucket │
     └────────────────────────────┘        └─────────────────────────┘
```

**Read the diagram for the absence.** There is no NAT gateway, no egress-only
gateway, and no route out of the private subnets at all.

---

## 3. What "the sandbox needs no network" is worth

`SANDBOX_NETWORK_POLICY` defaults to `none`, packages are pre-baked into
`infra/sandbox/Dockerfile`, and `container_provider.py` starts every execution
with `--network none`. The execution path therefore has no interface, no DNS, no
loopback to the host, and no route to filter.

**In dollars.** A NAT gateway is $0.045/hour plus $0.045/GB processed —
**$32.85/month per AZ before a single byte**, $65.70 for the conventional
two-AZ pattern. But the saving is larger than one line item, because a sandbox
with egress is not a sandbox with a NAT gateway; it is a sandbox with a NAT
gateway *and a policy enforcement point*. `docs/THREAT_MODEL.md`:

> The `container` driver has no per-host egress filter — Docker offers none, and
> the honest implementations are a proxy or an iptables sidecar.

So the real alternative bill is a NAT gateway, a filtering proxy instance
(~$12/month for a `t4g.small` plus the work of operating it), and the
private-subnet rearrangement that comes with them — call it **$45–78/month and
one new component you have to keep correct.**

Except that it is *not only* the sandbox that would move. With the sandbox
needing no egress, the only thing in this deployment that must reach the
internet is the API process (`api.openai.com` — `Settings` refuses to construct
without a working `OPENAI_API_KEY`, so this is not optional — plus ECR, Secrets
Manager, CloudWatch Logs). One process needing outbound 443 is satisfied by a
public subnet, a public IP and a security group that accepts nothing but the
ALB. That is why `infra/aws/network.tf` has no NAT gateway *anywhere*, and why
interface VPC endpoints (~$7.30/month each, ~$29/month for the four you would
want) were also rejected: they would remove traffic from a route the host has to
keep open regardless.

**In attack surface**, which is the part that matters more. `docs/THREAT_MODEL.md`
names the residual risk of this feature precisely once, and it is egress:

> A sandbox holds whatever the user asked the agent to analyse. With
> `SANDBOX_NETWORK_POLICY=open` it also holds a socket. […] No sandbox escape is
> required, no provider bug is involved.

`--network none` deletes that risk rather than mitigating it. It also deletes
two AWS-specific ones for free: generated code cannot reach `169.254.169.254`
(IMDS, the instance profile) or `169.254.170.2` (the ECS task-role credential
endpoint), because there is no interface on which to address them. That is a
stronger guarantee than any IAM policy, and it is why the host's instance
profile holding S3 write permission is tolerable.

---

## 4. Where everything lives

### The API

ECS task, `bridge` networking, fixed host port 8000, on the single EC2 host.
Bind-mounts two host paths:

| Host path | Container path | Why |
|---|---|---|
| `/var/run/docker.sock` | `/var/run/docker.sock` | the `container` sandbox driver |
| `/var/lib/grain` | `/var/lib/grain` | **must be the same string** |

That second row is the detail that decides whether the sandbox works at all.
`container_provider.py` builds `-v {session_dir}:/workspace` and hands it to the
**host** daemon, which resolves the bind source against the **host** filesystem.
If the API container saw the session directory at `/data/sandboxes/abc` while
the host knew it as `/var/lib/grain/sandboxes/abc`, Docker would mount an
empty host directory and every execution would silently run against nothing.

The task runs as `root`, because `/var/run/docker.sock` is `root:docker` and the
`docker` group's gid is not stable across ECS AMI releases. This is the honest
cost of the container driver, and `infra/aws/iam.tf` opens with what it does and
does not mean: **a process holding the Docker socket is root on the host**, so
the IAM separation in this deployment is a boundary against misconfiguration and
credential sprawl, not against an attacker with code execution in the API
container. What protects credentials from *generated* code is `--network none`.

### PostgreSQL

RDS `db.t4g.small`, PostgreSQL 16, Single-AZ, in private subnets with no route
to the internet gateway, reachable only from the application security group,
`rds.force_ssl = 1`, storage encrypted with the deployment CMK, 7-day automated
backups with PITR.

`docs/ARCHITECTURE.md` names PostgreSQL the production system of record and
`docs/THREAT_MODEL.md` lists SQLite among the known MVP limitations
("not multi-process production transports"). `DATABASE_URL` is the only wiring:
`app/database.py` branches on the `sqlite` prefix and otherwise hands the URL
straight to SQLAlchemy with `pool_pre_ping=True`.

**No pgvector**, despite `infra/compose.yaml` using the `pgvector/pgvector:pg16`
image locally. Chunk and memory embeddings are `LargeBinary` columns
(`app/models.py:261`, `app/models.py:799`) scored in Python and bounded by
`RETRIEVAL_VECTOR_CANDIDATE_CAP` / `MEMORY_RECALL_CANDIDATE_CAP`. That is why the
*API host's* RAM is the constraint that matters and the database can be small.

The URL uses `postgresql+psycopg://` (psycopg 3), so **the API image must be
built with the `postgres` extra** — `pip install -e "apps/api[postgres]"`.
Without it SQLAlchemy cannot find a driver at import time.

### `objects_dir` — read this before assuming it is S3

It is not S3, and it cannot be today. `objects_dir` is a `Path` and both writers
treat it as one: `services/ingestion.py:302` calls
`directory.mkdir(parents=True, exist_ok=True)` and `services/analytics.py:229`
writes dataset snapshots the same way. **There is no object-store seam in the
codebase.** The `S3_ENDPOINT` / `S3_BUCKET` lines in `.env.example` sit under a
"Production adapters" comment and correspond to no field on `Settings`, so
setting them configures nothing.

So:

- **Originals and dataset snapshots live on an encrypted gp3 volume** attached to
  the host at `/var/lib/grain/objects`, separate from the root volume so
  replacing the instance does not destroy them. `prevent_destroy` is set on it.
- **The S3 bucket is the backup**, not the store: a nightly `aws s3 sync` at
  07:10 UTC, with bucket versioning on, KMS encryption, a lifecycle rule to IA at
  30 days, and — deliberately — no `s3:DeleteObject` in the instance role, so a
  deletion on the host cannot propagate into the backup.
- Backups are aligned to one recovery point, which is what `docs/RUNBOOK.md`
  asks for: RDS backup window 07:00–08:00, DLM volume snapshot 07:15, object
  sync 07:10.
- **When an object-store backend does land**, the bucket, the CMK, the IAM policy
  and the free S3 gateway endpoint are already provisioned and `OBJECTS_DIR`
  becomes a URI.

### Container images

Two ECR repositories, both `IMMUTABLE`-tagged, scan-on-push, KMS-encrypted:

- `grain/api` — the FastAPI application. Pulled by the ECS task.
- `grain/sandbox` — `infra/sandbox/Dockerfile`. Pulled by the **host Docker
  daemon**, never by a task: no ECS task references it, so nothing would fetch
  it automatically. `user_data` installs a `grain-sandbox-image.service` unit
  that reads the image URI from an SSM parameter and pulls it at boot.

Immutable tags mean a deploy is a new tag and a rollback is an older tag, rather
than a question you answer with `docker inspect`.

---

## 5. Vercel ↔ API across origins

The web app is on Vercel; the API is on the ALB. The session cookie is
`HttpOnly; Secure; SameSite=None; Path=/` with **no `Domain`**
(`services/auth/sessions.py:157`). Four things have to line up.

**1. Put the web app on the same apex domain as the API.** This is the
recommendation and it is load-bearing, not cosmetic.

`SameSite` is computed on the registrable domain (eTLD+1). With
`app.example.com` and `api.example.com` the two are **same-site** — the cookie
is first-party, and Safari's ITP and Chrome's third-party cookie restrictions do
not touch it. With the web app left on `something.vercel.app` the two are
**cross-site**, the session cookie is a third-party cookie on a subresource
request, and **Safari blocks it by default** — which presents as users being
silently logged out on every page load, on one browser, in a way that never
reproduces in Chrome dev. Attach a custom domain in Vercel.

`SameSite=None` stays regardless. It is required by `config.py`'s design and
`_guard_auth` refuses to boot with `SESSION_COOKIE_SECURE=false` outside
development. On a shared apex it is simply redundant rather than wrong.

**2. Leave `SESSION_COOKIE_DOMAIN` unset.** A `Domain` attribute cannot make a
cross-site cookie work, and on a shared apex it would widen a session cookie to
every subdomain — preview deployments, static hosts, anything you add later. The
cookie only ever needs to return to the API host, and host-only is what it needs
to be. `infra/aws/ecs.tf` does not set it.

**3. `WEB_ORIGIN` must name the exact origin.** `CORSMiddleware` is configured
with `allow_origins=settings.allowed_web_origins` and `allow_credentials=True`
(`app/main.py:65`), so the API echoes the exact origin — a wildcard would be
rejected by every browser for a credentialed request, and the code correctly
never emits one. Comma-separate if you need more than one. `WEB_ORIGIN` also
feeds `primary_web_origin`, which is where password-reset links and the
post-login redirect point.

**4. CSRF still applies, and it is why the header exists.** `SameSite=None`
re-opens CSRF; the answer is the double-submit `X-CSRF-Token` header compared
in constant time against `session.csrf_secret`. Being a custom header, it forces
a CORS preflight, which a cross-origin `<form>` cannot satisfy. The ALB forwards
`OPTIONS` and `CORSMiddleware` answers it, so nothing is needed here except not
breaking it: `drop_invalid_header_fields = true` is set on the ALB precisely
because that comparison is on an attacker-controlled header.

**On the Vercel side**, set `NEXT_PUBLIC_API_URL=https://api.example.com`.
Note it is needed **at build time**: `apps/web/next.config.ts` inlines it into
the page CSP's `connect-src` and `frame-src`, so an app built without it ships a
CSP that blocks its own API and the failure looks like a network error rather
than a policy one.

---

## 6. Secrets

Four Secrets Manager entries, each mapping to exactly one `Settings` field.
pydantic-settings maps field names to upper-cased environment variables with no
alias table, so the environment variable name *is* the field name in caps.

| Secrets Manager | Env var | `Settings` field | Notes |
|---|---|---|---|
| `grain/database-url` | `DATABASE_URL` | `database_url: str` | written by Terraform |
| `grain/openai-api-key` | `OPENAI_API_KEY` | `openai_api_key: Optional[SecretStr]` | placeholder; set by hand |
| `grain/integrations-key` | `INTEGRATIONS_ENCRYPTION_KEY` | `integrations_encryption_key: Optional[SecretStr]` | Fernet; **unrecoverable if lost** |
| `grain/google-login-client-secret` | `GOOGLE_LOGIN_CLIENT_SECRET` | `google_login_client_secret: Optional[SecretStr]` | placeholder |

Three of the four are created as `REPLACE_ME` with `ignore_changes`, so their
real values never enter Terraform state:

```bash
aws secretsmanager put-secret-value --secret-id grain/openai-api-key \
  --secret-string 'sk-...'

python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' \
  | xargs -I{} aws secretsmanager put-secret-value \
      --secret-id grain/integrations-key --secret-string '{}'
```

`DATABASE_URL` is the exception, and the reason is a code constraint rather than
a preference: the application consumes one URL string, so RDS-managed rotation
(which produces a `{username, password}` JSON secret) would need an entrypoint
to assemble the URL. Terraform generates the password instead, which puts it in
state — **so the state backend must be encrypted S3**, and `infra/aws/versions.tf`
says so at the top.

**Everything else is plain task-definition environment**, in
`infra/aws/ecs.tf`'s `local.base_environment`. That split is not stylistic: task
definitions are readable by anyone with `ecs:DescribeTaskDefinition`, a much
wider set of principals than the four secret ARNs enumerated in the execution
role. Add non-secret overrides through the `extra_environment` variable rather
than editing `ecs.tf`.

Two environment values are worth calling out:

- `OBJECTS_DIR` and `SANDBOX_WORKDIR` are **absolute**, and must be.
  `config.py`'s `project_root()` walks up looking for a directory containing both
  a `Makefile` and `apps/`; the anchoring validators use it for relative paths.
  Absolute paths bypass anchoring entirely, which removes a dependency on the
  image's layout.
- `APP_ENV=production` is not a label. Every relaxation in `config.py` —
  `DEV_AUTO_LOGIN`, `DEV_USER`, `MODEL_PROVIDER=scripted`,
  `SANDBOX_PROVIDER=subprocess|fake`, `SESSION_COOKIE_SECURE=false` — is refused
  unless it says `development` or `test`.

### Secrets Manager, not Parameter Store

SSM Parameter Store `SecureString` would cost $0 against Secrets Manager's
$0.40/secret/month, so this deployment pays **$1.60/month** for the difference.
It buys resource policies, native rotation if `DATABASE_URL` is ever restructured
to allow it, and — the reason that actually decided it — a distinct ARN per
secret so the execution role's grant is four enumerated ARNs rather than a path
prefix. A `grain/*` prefix wildcard would silently grant every secret anyone
adds under it later. One SSM `String` parameter *is* used, for the sandbox image
URI, because it is not a secret and it is read by the host's boot script.

---

## 7. Migrations on deploy

`make migrate` is `cd apps/api && ../../.venv/bin/alembic upgrade head`. On AWS
it is the same command, in the same image as the API, as a one-off ECS task
(`grain-migrate`) with its own task role that holds no permissions and, in
particular, no Docker socket.

```bash
CLUSTER=$(terraform -chdir=infra/aws output -raw ecs_cluster_name)

TASK=$(aws ecs run-task --cluster "$CLUSTER" \
        --task-definition grain-migrate --launch-type EC2 \
        --query 'tasks[0].taskArn' --output text)

aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" \
  --query 'tasks[0].containers[0].exitCode'   # must be 0
```

The migration task carries `OPENAI_API_KEY` even though a migration has no use
for it, because `Settings` refuses to construct without one and `alembic/env.py`
calls `get_settings()`. That exact failure has already happened in this
repository once — it is why `config.py` anchors `env_file` to the repo root.

**Order, and the window it leaves.** Migrate first, then roll the service. Between
those two steps the *old* code is running against the *new* schema, and with
`deployment_minimum_healthy_percent = 0` that window is short but real. Either
keep migrations additive (add columns/tables, backfill, drop in a later release),
or take an explicit window:

```bash
aws ecs update-service --cluster "$CLUSTER" --service grain-api --desired-count 0
# ... run the migration ...
aws ecs update-service --cluster "$CLUSTER" --service grain-api --desired-count 1
```

---

## 8. The sandbox image

`make sandbox-image` builds it locally. For AWS it has to be built for the
**host's architecture** (`m7g.large` is arm64) and pushed to ECR:

```bash
REG=$(terraform -chdir=infra/aws output -raw ecr_sandbox_repository_url)
TAG=$(date -u +%Y-%m-%d)-$(git rev-parse --short HEAD)

aws ecr get-login-password | docker login --username AWS --password-stdin "${REG%%/*}"
docker buildx build --platform linux/arm64 -t "$REG:$TAG" --push infra/sandbox
```

Then point the host at it and make it pull. The image URI lives in an SSM
parameter so that rolling it does not mean replacing the instance:

```bash
aws ssm put-parameter --name /grain/sandbox-image --overwrite \
    --type String --value "$REG:$TAG"

aws ssm send-command --document-name AWS-RunShellScript \
    --targets "Key=instanceids,Values=$(terraform -chdir=infra/aws output -raw instance_id)" \
    --parameters 'commands=["/usr/local/bin/grain-sandbox-image"]'
```

Finally set `sandbox_image_tag` in `terraform.tfvars` and apply, which updates
`SANDBOX_CONTAINER_IMAGE` on the task definition.

**Do those in that order.** If the task definition points at an image the host
has not pulled, `ContainerProvider` fails its `docker image inspect` preflight
with *"sandbox image '…' is not available"* — a legible error rather than a
mystery, but still an outage of the execution feature.

Remember what this image *is*: with `SANDBOX_NETWORK_POLICY=none` nothing can be
installed at runtime, so `infra/sandbox/Dockerfile` is the package policy.
"The agent needs seaborn" is a rebuild, a push, an SSM pull and an apply. The
ECR lifecycle policy keeps 20 tagged images so the previous one is always a
rollback away.

---

## 9. Cost

**Stated scale**, because a cost estimate without one is a number:
25 daily-active users across a handful of workspaces, ~5,000 chat turns/month,
~200 sandbox executions/day, ~30 GB of uploaded originals, ~50 GB database,
`us-east-1`, on-demand, no savings plan. List prices as of August 2026 — verify
against the pricing pages before quoting them at anyone.

| Line | Spec | $/month |
|---|---|---:|
| EC2 | `m7g.large`, 730 h × $0.0816 | 59.57 |
| EBS root | 60 GB gp3 × $0.08 | 4.80 |
| EBS objects volume | 100 GB gp3 × $0.08 | 8.00 |
| EBS snapshots | ~60 GB-mo × $0.05 | 3.00 |
| ALB | 730 h × $0.0225 + ~1 LCU | 22.27 |
| RDS instance | `db.t4g.small` Single-AZ, 730 h × $0.032 | 23.36 |
| RDS storage | 50 GB gp3 × $0.115 | 5.75 |
| S3 | ~60 GB with versions, mixed Standard/IA | 1.50 |
| ECR | ~10 GB × $0.10 | 1.00 |
| Secrets Manager | 4 × $0.40 | 1.60 |
| KMS | 1 CMK + requests | 1.10 |
| CloudWatch Logs | ~5 GB ingest + storage | 2.65 |
| Container Insights | custom metrics for a 1-instance cluster | ~10.00 |
| CloudWatch alarms | 6 × $0.10 | 0.60 |
| Route 53 | 1 hosted zone | 0.50 |
| Data transfer out | ~20 GB, under the 100 GB free tier | 0.00 |
| **NAT gateway** | **not provisioned** | **0.00** |
| **Total** | | **≈ $146** |

**What dominates it.** Within AWS: compute, load balancer and database are
**$105 of $146 — 72%** — and of those three, the EC2 instance alone is 41%. Every
other line is noise, which is worth knowing before anyone spends an afternoon on
S3 lifecycle rules. The single biggest *discretionary* line is Container
Insights at ~$10/month; set `containerInsights = "disabled"` on the cluster if
CloudWatch Logs plus the six alarms are enough.

**What dominates the actual bill**, though, is almost certainly not AWS. Every
chat turn is an OpenAI call, code generation is bounded at
`OPENAI_CODEGEN_MAX_OUTPUT_TOKENS = 16000`, `WEB_SEARCH_ENABLED` is on by
default, and ingestion embeds every chunk. At 5,000 turns/month the model bill
plausibly exceeds $146 on its own. I am not quoting a per-token figure for
`gpt-5.5` that I cannot verify here — price it from your own usage dashboard
before treating this table as the budget.

**Runners-up, priced:**

| Change | Δ $/month | What you get or lose |
|---|---:|---|
| Sandbox egress (`open`/`allowlist`) | **+45 to +78** | NAT gateway, a filtering proxy, and `THREAT_MODEL.md`'s named residual risk |
| Fargate API + EC2 executor | **+36** | plus a `SandboxProvider` driver that does not exist |
| Multi-AZ RDS | **+23** | removes one of this topology's two single points of failure |
| Drop the ALB for an EIP + Caddy | **−22** | certificate renewal on the sandbox host |
| Container Insights off | **−10** | fewer dashboards |
| `SANDBOX_PROVIDER=e2b`, smaller host | **−30** | user documents leave the account |
| Plain EC2 instead of ECS | **0** | lose the task-role split, `run-task` migrations, rolling deploys |
| 1-year compute savings plan | **−~25** | commitment |

---

## 10. Runbook

### First apply

```bash
# 1. State backend. It will hold the RDS password; encrypt it.
aws s3api create-bucket --bucket grain-tfstate --region us-east-1
aws s3api put-bucket-versioning --bucket grain-tfstate \
  --versioning-configuration Status=Enabled

# 2. Certificate for api.example.com, in the same region as the ALB.
aws acm request-certificate --domain-name api.example.com \
  --validation-method DNS

# 3. Configure and apply. Uncomment the backend block in versions.tf first.
cd infra/aws
cp terraform.tfvars.example terraform.tfvars   # edit it
terraform init -backend-config=bucket=grain-tfstate \
               -backend-config=key=prod/terraform.tfstate \
               -backend-config=region=us-east-1 -backend-config=encrypt=true
terraform apply
```

The first apply fails at the ECS service if no image has been pushed yet. Push
both images (§8 for the sandbox, appendix for the API), then re-apply.

```bash
# 4. Real secret values (§6).
# 5. DNS: point api.example.com at the alb_dns_name output (ALIAS in Route 53).
# 6. Migrate (§7), then confirm.
curl -sS https://api.example.com/health
```

### Deploy a new version

```bash
TAG=$(date -u +%Y-%m-%d)-$(git rev-parse --short HEAD)
REG=$(terraform -chdir=infra/aws output -raw ecr_api_repository_url)

aws ecr get-login-password | docker login --username AWS --password-stdin "${REG%%/*}"
docker buildx build --platform linux/arm64 -t "$REG:$TAG" --push -f apps/api/Dockerfile .

# migrate first (§7), then:
terraform -chdir=infra/aws apply -var="api_image_tag=$TAG"
aws ecs update-service --cluster grain-cluster --service grain-api \
  --task-definition grain-api --force-new-deployment
```

Expect **30–60 seconds of downtime per deploy**. The fixed host port means two
tasks cannot coexist, so ECS stops the old one first. That is the deliberate
choice explained in §1: brief honest downtime beats two API processes briefly
running two in-process executors over the same `runs` table.

### Roll back

Re-apply with the previous `api_image_tag`. ECR tags are immutable, so the
earlier image is exactly what shipped. If the rollback crosses a migration, roll
the schema back explicitly first (`alembic downgrade <rev>` through the migrate
task's command override) — nothing does that automatically.

### Get a shell

```bash
aws ssm start-session --target $(terraform -chdir=infra/aws output -raw instance_id)
```

There is no SSH key pair and no inbound SSH rule anywhere in this configuration.
Session Manager is audited in CloudTrail; SSH would not be.

### Re-run the boot script after editing it

`user_data_replace_on_change = false`, so editing `user_data.sh.tftpl` and
applying updates the attribute without destroying the host. Apply it, then:

```bash
aws ssm send-command --document-name AWS-RunShellScript \
  --targets "Key=instanceids,Values=$INSTANCE_ID" \
  --parameters 'commands=["cloud-init clean --logs && cloud-init init && cloud-init modules --mode=final"]'
```

or simply reboot the instance. Check `/var/log/grain-bootstrap.log`.

### Recover from instance loss

The failure mode this topology has, stated honestly. **RTO is manual and roughly
20–30 minutes; RPO is 5 minutes for the database (PITR) and up to 24 hours for
objects written since the last sync** — though the objects themselves survive on
the EBS volume unless the volume is also lost.

```bash
terraform -chdir=infra/aws taint aws_instance.app
terraform -chdir=infra/aws apply
```

The data volume carries `prevent_destroy`, so it survives; `aws_volume_attachment`
reattaches it and `user_data` finds it by NVMe serial and mounts it without
reformatting (`blkid` guards the `mkfs`). If the volume itself is gone, restore
the newest DLM snapshot into a new volume, then `terraform import` it —
or restore objects from S3, which has versioning.

If the AZ is gone: change `aws_subnet.public[0]`'s AZ, restore the volume
snapshot into the new AZ, and re-apply. The database is Single-AZ by default too;
set `db_multi_az = true` (+$23/month) if that is unacceptable.

### Everything else

`docs/RUNBOOK.md` still applies for failed ingestion, stuck runs, graph
projection, dataset failures and app publication. Two amendments for AWS:

- "Process logs remain on stdout" now means the `/grain/api` CloudWatch log
  group.
- "Back up the database and object bucket as one logical recovery point" is
  implemented as the aligned 07:00–07:15 UTC window described in §4.

---

## 11. What this does not do

Stated plainly rather than discovered later.

1. **The Terraform has never been applied.** No AWS account was available in this
   environment. It is formatted and validated (§12) and internally consistent,
   but `terraform plan` against a real account will surface things static
   validation cannot — AMI/architecture mismatches, service quotas, an ACM
   certificate in the wrong region. Treat the first apply as a review, not a
   rubber stamp.
2. **`apps/api/Dockerfile` does not exist.** Nothing in this repository builds
   the API image. That file belongs to the API track, not this one, so it is
   specified in the appendix rather than written here. Until it exists,
   `api_image_tag` names nothing and the ECS service will not start a task.
3. **`objects_dir` is not S3** and cannot be without a code change (§4). The
   bucket provisioned here is a backup target.
4. **One instance, one AZ, manual recovery.** For the correctness reason in §1,
   not for cost. Fixing it properly means a queue transport, not more Terraform.
5. **No WAF, no CloudFront, no Shield Advanced.** The ALB is directly exposed.
   Rate limiting exists in the application for auth endpoints only
   (`AUTH_RATE_LIMIT_ATTEMPTS`). A WAF with a rate rule is ~$8/month plus $0.60
   per million requests and is the obvious next addition.
6. **No egress filtering from the API host.** It has outbound 443 to anywhere.
   `docs/THREAT_MODEL.md` already flags that production egress should run through
   a policy-enforcing proxy to close the DNS-rebinding TOCTOU gap in tool fetches;
   that proxy is not built here. The *sandbox* needs none of this — it has no
   route at all.
7. **No blue/green.** Deploys have a downtime window (§10).
8. **Cost figures are list prices, unverified against a live account**, and the
   OpenAI bill — which probably dominates — is not estimated at all (§9).

---

## 12. Verification performed

```
$ terraform version
terraform not found
```

The `terraform` binary is not installed in this environment. OpenTofu 1.12.5 was
downloaded to a scratch directory and used instead — same HCL, same
`hashicorp/aws` provider — rather than reporting the configuration unchecked:

```
$ tofu init -backend=false -input=false
- Installed hashicorp/aws v5.100.0 (signed, key ID 0C0AF313E5FD9F80)
OpenTofu has been successfully initialized!

$ tofu fmt -recursive -check -diff
$ echo $?
0

$ tofu validate
Success! The configuration is valid.
```

`user_data.sh.tftpl` was additionally rendered through `templatefile` with
representative values and the result checked with `bash -n`, because
`tofu validate` does not evaluate templates; the generated CloudWatch agent
config was parsed as JSON to confirm the `$${aws:InstanceId}` escape survives
both the Terraform template pass and the shell heredoc.

This track added no Python and no TypeScript, so `ruff`, `mypy`, `pytest` and
the web gates are unaffected by it. Both were run anyway, to show the tree is
not red because of these files:

```
$ .venv/bin/ruff check apps/api
All checks passed!

$ .venv/bin/mypy apps/api/app
Success: no issues found in 96 source files
```

---

## Appendix: the API image contract

`apps/api/Dockerfile` is owned by the API track and does not exist yet. The
Terraform in `infra/aws/` assumes an image satisfying this contract; if it lands
differently, `ecs.tf`'s `workingDirectory` and command need to follow.

```dockerfile
FROM python:3.12-slim-bookworm

# The container sandbox driver shells out to SANDBOX_DOCKER_BINARY ("docker")
# to start SIBLING containers on the host daemon via the bind-mounted socket.
# The CLI is required; the daemon is not.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
      -o /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
      https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
 && apt-get purge -y --auto-remove gnupg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# The Makefile matters: config.py's project_root() looks for a directory holding
# both a Makefile and apps/, and falls back to cwd() when it finds neither. The
# task definition sets every path setting absolutely so this is belt-and-braces,
# but a deterministic project_root() is worth the 2 KB.
COPY Makefile ./
COPY apps/api ./apps/api

# The postgres extra installs psycopg 3, which is what postgresql+psycopg:// needs.
RUN pip install --no-cache-dir -e "apps/api[postgres]"

EXPOSE 8000

# ONE worker. Background work is in-process and startup recovery runs per
# process; a second worker would run a second executor over the same runs table.
# No --reload.
CMD ["uvicorn", "app.main:app", "--app-dir", "apps/api", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

Build it for the host's architecture (`--platform linux/arm64` for `m7g.large`)
and push it to the `grain/api` repository. The same image serves the
migration task, which overrides `workingDirectory` to `/app/apps/api` and the
command to `alembic upgrade head`.
