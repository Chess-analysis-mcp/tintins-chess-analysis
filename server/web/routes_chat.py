"""In-browser chat route (Phase 6): POST /api/chat -> headless Claude Code."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from server import claude_bridge

router = APIRouter()


class ChatBody(BaseModel):
    question: str
    fen: str | None = None  # the board the user is viewing
    last_move: str | None = None  # the move in question
    move_fen: str | None = None  # the position that move was played from
    session_id: str | None = None


@router.post("/chat")
def chat(body: ChatBody) -> JSONResponse:
    """Answer a position-aware 'why?' / 'what now?' question on the user's Claude subscription."""
    if not body.question.strip():
        return JSONResponse({"error": "Empty question."}, status_code=400)
    try:
        res = claude_bridge.ask(
            body.question,
            fen=body.fen,
            last_move=body.last_move,
            move_fen=body.move_fen,
            session_id=body.session_id,
        )
    except claude_bridge.ChatError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse(res)
