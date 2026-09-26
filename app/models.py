"""数据模型。

编号规则（对人类友好、可直接印在标签底稿上）：
- 原料：ING-0001
- 配方：REC-0001
- 配方条目：ENT-000001（每条组成行都有独立条目编号，全局唯一）
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SeqCounter(Base):
    """发号器：按前缀维护自增序号，与业务写入同事务提交。"""

    __tablename__ = "seq_counters"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Ingredient(Base):
    __tablename__ = "ingredients"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # ING-0001
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    allergens: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)


class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # REC-0001
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, default=utcnow, onupdate=utcnow
    )

    entries: Mapped[list["RecipeEntry"]] = relationship(
        back_populates="recipe",
        cascade="all, delete-orphan",
        order_by="RecipeEntry.position",
    )


class RecipeEntry(Base):
    """配方中的一行组成：引用一种原料或一个子配方，带独立条目编号。"""

    __tablename__ = "recipe_entries"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)  # ENT-000001
    recipe_id: Mapped[str] = mapped_column(
        ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)  # 配方内行号，从 1 开始
    ref_type: Mapped[str] = mapped_column(String(16), nullable=False)  # ingredient | recipe
    ref_id: Mapped[str] = mapped_column(String(16), nullable=False)  # ING-xxxx / REC-xxxx
    quantity: Mapped[float | None] = mapped_column(nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)

    recipe: Mapped[Recipe] = relationship(back_populates="entries")
