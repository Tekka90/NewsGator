"""Category taxonomy CRUD (admin). Taxonomy is customizable (SPEC §8)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_user
from app.api.schemas import CategoryIn, CategoryOut, CategorySuggestionAction, CategorySuggestionOut
from app.core.config import settings
from app.core.db import get_session
from app.models import Category
from app.services import category_suggestions

router = APIRouter(prefix="/categories", tags=["categories"], dependencies=[Depends(admin_user)])


@router.get("")
async def list_categories(session: AsyncSession = Depends(get_session)) -> list[CategoryOut]:
    rows = await session.scalars(select(Category).order_by(Category.name))
    return [CategoryOut.model_validate(c) for c in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_category(
    body: CategoryIn, session: AsyncSession = Depends(get_session)
) -> CategoryOut:
    exists = await session.scalar(select(Category).where(Category.name == body.name))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Category already exists")
    cat = Category(name=body.name)
    session.add(cat)
    await session.commit()
    await session.refresh(cat)
    return CategoryOut.model_validate(cat)


# Declared before /{category_id} — a literal "suggestions" segment must not be
# swallowed by the parameterized route (same convention as /stories/share-languages).
@router.get("/suggestions")
async def list_suggestions(
    session: AsyncSession = Depends(get_session),
) -> list[CategorySuggestionOut]:
    """Recurring LLM-proposed categories not yet in the taxonomy (SPEC §8)."""
    proposals = await category_suggestions.list_proposals(
        session,
        min_articles=settings.category_suggestion_min_articles,
        window_days=settings.category_suggestion_window_days,
    )
    return [CategorySuggestionOut(**p.model_dump()) for p in proposals]


@router.post("/suggestions/accept", status_code=status.HTTP_201_CREATED)
async def accept_suggestion(
    body: CategorySuggestionAction, session: AsyncSession = Depends(get_session)
) -> CategoryOut:
    """Add the proposal's label as a real taxonomy category and clear the log."""
    name = body.label.strip()
    if not name:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "label is required")
    exists = await session.scalar(select(Category).where(Category.name == name))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "Category already exists")
    cat = Category(name=name)
    session.add(cat)
    await session.commit()
    await session.refresh(cat)
    await category_suggestions.clear_suggestions(session, body.normalized_text)
    return CategoryOut.model_validate(cat)


@router.post("/suggestions/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_suggestion(
    body: CategorySuggestionAction, session: AsyncSession = Depends(get_session)
) -> None:
    """Suppress a proposal cluster from future suggestion listings."""
    await category_suggestions.dismiss(session, body.normalized_text)


@router.patch("/{category_id}")
async def rename_category(
    category_id: int, body: CategoryIn, session: AsyncSession = Depends(get_session)
) -> CategoryOut:
    cat = await session.get(Category, category_id)
    if cat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")
    cat.name = body.name
    await session.commit()
    return CategoryOut.model_validate(cat)


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(category_id: int, session: AsyncSession = Depends(get_session)) -> None:
    cat = await session.get(Category, category_id)
    if cat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")
    if cat.name == "Uncategorized":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Cannot delete 'Uncategorized'")
    # SPEC §8: deleting moves items to 'Uncategorized' (applied to articles/stories in M3/M4)
    await session.delete(cat)
    await session.commit()
