from __future__ import annotations

from typing import TYPE_CHECKING, Dict

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from ..llm_tools import ToolContext, ToolSpec


def registry_tools(db: Session, context: ToolContext) -> Dict[str, ToolSpec]:
    """LLM tools for connected integration accounts."""
    from sqlalchemy import select

    from ...models import IntegrationAccount

    accounts = list(
        db.scalars(
            select(IntegrationAccount).where(
                IntegrationAccount.workspace_id == context.workspace_id,
                IntegrationAccount.status == "connected",
            )
        )
    )
    tools: Dict[str, ToolSpec] = {}
    for account in accounts:
        if account.provider == "google":
            from .gmail import gmail_tools

            tools.update(gmail_tools(account))
        elif account.provider == "strava":
            from .strava import strava_tools

            tools.update(strava_tools(account))
        elif account.provider == "garmin":
            from .garmin import garmin_tools

            tools.update(garmin_tools(account))
    return tools
