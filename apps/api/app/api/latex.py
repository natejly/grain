"""POST /api/latex/compile — server-side LaTeX compilation."""
from __future__ import annotations

import asyncio
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import Actor, get_actor
from ..config import get_settings
from ..schemas import ApiModel
from ..services.projects.compile import (
    CompileError,
    CompileResult,
    compile_latex,
    image_available,
)

router = APIRouter(prefix="/api/latex", tags=["latex"])


class FileInput(BaseModel):
    path: str
    content: str


class CompileRequest(BaseModel):
    engine: Literal["pdftex", "xetex"] = "pdftex"
    entry_path: str
    files: List[FileInput]


class CompileResponse(ApiModel):
    status: Literal["ok", "failed"]
    message: str
    log: str
    pdf_base64: Optional[str] = None


@router.post("/compile", response_model=CompileResponse)
async def compile_endpoint(
    body: CompileRequest,
    actor: Actor = Depends(get_actor),
):
    settings = get_settings()

    if not settings.latex_compile_enabled:
        raise HTTPException(
            status_code=503,
            detail="LaTeX compilation is not enabled on this server.",
        )

    provider: Literal["container", "subprocess"] = "container"
    image = settings.latex_compile_image

    if not image_available(image):
        if settings.is_dev_env:
            provider = "subprocess"
        else:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"The LaTeX image '{image}' is not available. "
                    "Run `make latex-image` to build it."
                ),
            )

    files_dicts = [{"path": f.path, "content": f.content} for f in body.files]

    try:
        result: CompileResult = await asyncio.to_thread(
            compile_latex,
            files_dicts,
            body.entry_path,
            engine=body.engine,
            image=image,
            timeout_seconds=settings.latex_compile_timeout_seconds,
            memory_mb=settings.latex_compile_memory_mb,
            cpus=settings.latex_compile_cpus,
            pids_limit=settings.latex_compile_pids_limit,
            provider=provider,
        )
    except CompileError as exc:
        # `from exc` so a compile failure is distinguishable from a fault in
        # the handler itself when this shows up in a traceback.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return CompileResponse(
        status=result.status,
        message=result.message,
        log=result.log,
        pdf_base64=result.pdf_base64,
    )
