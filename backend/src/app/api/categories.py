"""Category taxonomy CRUD (admin). Taxonomy is customizable (SPEC §8)."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import admin_user
from app.api.schemas import CategoryIn, CategoryOut
from app.core.db import get_session
from app.models import Category

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
