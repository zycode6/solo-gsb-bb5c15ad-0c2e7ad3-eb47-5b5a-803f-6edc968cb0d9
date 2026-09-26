"""请求 / 响应模型。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _normalize_allergens(values: list[str]) -> list[str]:
    """去除空白与空项，按出现顺序去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        item = raw.strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


# ---------- 原料 ----------

class IngredientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    allergens: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name 不能为空")
        return v

    _norm_allergens = field_validator("allergens")(_normalize_allergens)


class IngredientOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    allergens: list[str]
    created_at: datetime


# ---------- 配方 ----------

class EntryIn(BaseModel):
    """一行配方组成：引用原料或子配方。"""

    ref_type: Literal["ingredient", "recipe"]
    ref_id: str = Field(min_length=1, max_length=16)
    quantity: float | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, max_length=50)


class RecipeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    entries: list[EntryIn] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("name 不能为空")
        return v


class EntryOut(BaseModel):
    id: str  # 条目编号 ENT-xxxxxx
    position: int
    ref_type: str
    ref_id: str
    ref_name: str
    quantity: float | None
    unit: str | None


class RecipeOut(BaseModel):
    id: str
    name: str
    description: str
    created_at: datetime
    updated_at: datetime
    entries: list[EntryOut]


class RecipeSummary(BaseModel):
    id: str
    name: str
    entry_count: int
    updated_at: datetime


# ---------- 配方展开（标签核对） ----------

class PathStep(BaseModel):
    """展开路径上的一步：经由哪个配方的哪个条目，引用了什么。"""

    entry_id: str
    position: int
    recipe_id: str
    recipe_name: str
    ref_type: str
    ref_id: str
    ref_name: str
    quantity: float | None
    unit: str | None


class LeafPath(BaseModel):
    """一条从目标配方直达某原料叶子的完整来源路径。"""

    path: list[str]  # 依次经过的条目编号
    steps: list[PathStep]
    ingredient_id: str
    ingredient_name: str
    allergens: list[str]


class AllergenSource(BaseModel):
    path: list[str]
    ingredient_id: str
    ingredient_name: str


class AllergenSummaryItem(BaseModel):
    allergen: str
    sources: list[AllergenSource]


class ExpansionOut(BaseModel):
    recipe_id: str
    recipe_name: str
    leaf_count: int
    paths: list[LeafPath]
    allergen_summary: list[AllergenSummaryItem]
