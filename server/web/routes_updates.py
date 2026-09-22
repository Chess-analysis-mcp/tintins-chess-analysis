"""Update-notifier routes.

`GET /api/update-check` is the throttled GitHub-release lookup behind the board's "update available"
banner (fire-and-forget from the frontend, like /api/doctor). `POST /api/apply-update` stages a
one-click update for self-updatable channels (git / zip) by writing a sentinel the launcher applies
on the next start. Both are best-effort and never raise to the page.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server.core import updates

router = APIRouter()


@router.get("/update-check")
def get_update_check(force: bool = False) -> dict:
    """Is a newer release out? Returns current/latest/severity/channel; never raises (offline ->
    update_available False).

    `force=1` skips the throttle/cache: that's the Settings -> About button, where the user asked
    just now and a stale "you're up to date" would be a wrong answer. The automatic banner check
    leaves it off so page loads stay cheap and we don't hammer the GitHub API.
    """
    try:
        return updates.check_for_update(force=force)
    except Exception:  # noqa: BLE001 - the banner must never break the page
        return {"update_available": False}


class ApplyUpdateBody(BaseModel):
    # True when this session has no launcher to apply a staged update on its next start (a git
    # clone run with `uv run python scripts/run_web.py`, or the board behind the MCP server), so
    # the update has to happen now. The launcher-started app leaves it False and uses the sentinel,
    # which is the safer path: nothing is running from the files being replaced.
    now: bool = False


@router.post("/apply-update")
def post_apply_update(body: ApplyUpdateBody | None = None) -> JSONResponse:
    """Apply a one-click update (git + zip channels). The read-only `.app` can't self-update -> 409.

    Default: write a sentinel the launcher consumes on the next start; the user just reopens the
    app. With `{"now": true}`: update this checkout immediately (see `updates.apply_now`), for
    installs that were not started by one of our launchers.
    """
    if not updates.can_self_update():
        return JSONResponse(
            {"ok": False, "error": "This install can't self-update; download the latest from Releases."},
            status_code=409,
        )
    try:
        if body is not None and body.now:
            result = updates.apply_now()
            return JSONResponse(result, status_code=200 if result.get("ok") else 409)
        return JSONResponse(updates.request_update())
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
