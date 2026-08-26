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
