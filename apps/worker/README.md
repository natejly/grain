# Worker adapter

The development server uses FastAPI background tasks with the same idempotent task
entrypoints exported by `app.worker.tasks`. A production deployment should bind
these entrypoints to its Redis-backed worker and lease/retry policy without
changing domain behavior.

