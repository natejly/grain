"""What a failed run is allowed to say to the person who was waiting for it."""

from __future__ import annotations


class UserFacingError(Exception):
    """Marker: this exception's message was written to be read by a person.

    `_fail_run` catches everything an agent turn can raise, and nearly all of it
    is internal — a driver error, a bug, a database complaint whose text is SQL
    and bound parameters. None of that belongs on a user's screen. It says
    nothing they can act on, and it discloses schema, identifiers and query
    shape to anyone who can read the thread. `run.error` is published twice
    over: into the `run.failed` event the browser streams, and into the
    member-facing Inbox.

    A few failures are the opposite, and their message IS the answer.
    `OrgBoundExceeded` says so in its own docstring: telling someone "the
    request failed" when the honest answer is "your organization does not allow
    this" sends them off debugging the wrong thing. Those inherit this, and
    their message passes through unchanged.

    Inherit it only when the message is written for whoever will read it: no
    identifiers, no schema, no exception text quoted from a library. If a
    message needs an operator to interpret it, it is not user-facing — log it
    and let this module say something honest instead.
    """


#: What is said when the failure was not written for anyone to read. The run id
#: is included deliberately: the reader already owns it, it is the handle an
#: operator needs to find the real detail in the log, and it turns "it broke"
#: into something a support conversation can start from.
GENERIC_RUN_FAILURE = (
    "The assistant could not finish this turn. Try again — if it keeps "
    "happening, quote run {run_id} to your workspace owner."
)


def user_facing_message(exc: BaseException, *, run_id: str) -> str:
    """The safe rendering of `exc` for a client, and the only one they get.

    Deliberately allow-list shaped rather than sanitiser shaped: there is no
    reliable way to scrub identifiers out of arbitrary exception text, so the
    default is to say nothing about the cause at all and let the log carry it.
    An exception opts in by type, not by inspection of its message.
    """
    if isinstance(exc, UserFacingError):
        text = str(exc).strip()
        if text:
            return text[:1000]
    return GENERIC_RUN_FAILURE.format(run_id=run_id)
