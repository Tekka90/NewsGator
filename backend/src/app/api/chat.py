"""Chatbot API (SPEC §10): RAG question-answering over the story archive.

`POST /api/chat` — one stateless turn. The frontend keeps conversation history
locally; each request retrieves grounding stories fresh from the archive.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_user
from app.core.db import get_session
from app.models import User
from app.services import chat

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)


class ChatStoryOut(BaseModel):
    id: int
    title: str
    category: str
    image_url: str | None
    last_updated_at: datetime
    source_hosts: list[str]
    # exact cosine of question vs story centroid; None when no retrieval ran
    similarity: float | None
    cited: bool  # the LLM cited this story in its answer


class ChatOut(BaseModel):
    answer: str
    stories: list[ChatStoryOut]
    latency_ms: int


@router.post("")
async def ask(
    body: ChatIn,
    user: User = Depends(current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatOut:
    if not chat.is_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Chat is disabled")
    try:
        result = await chat.ask(
            session, body.question, user_id=user.id, username=user.username
        )
    except chat.ChatError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    return ChatOut(**result)
