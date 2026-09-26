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


# ---------- 标签预演（整体替换不落库，比较过敏原变化） ----------

class PreviewPathStep(BaseModel):
    """展开路径上的一步（位置视图）。

    预演不落库、不分配条目编号，因此路径以每层条目位置标识；
    已存在的条目仍附带其条目编号便于与现状对照，根配方的预演行 entry_id 为 null。
    """

    entry_id: str | None
    position: int
    recipe_id: str
    recipe_name: str
    ref_type: str
    ref_id: str
    ref_name: str
    quantity: float | None
    unit: str | None


class PreviewLeaf(BaseModel):
    """一条从根配方直达某原料叶子的完整来源路径，路径由各层条目位置组成。"""

    path: list[int]  # 依次经过的条目位置（每层从 1 开始）
    steps: list[PreviewPathStep]
    ingredient_id: str
    ingredient_name: str
    allergens: list[str]


class PreviewSource(BaseModel):
    """某过敏原的一条来源：来源原料 + 以位置组成的路径标识（含逐步明细）。"""

    path: list[int]
    steps: list[PreviewPathStep]
    ingredient_id: str
    ingredient_name: str


class PreviewAllergenSummaryItem(BaseModel):
    allergen: str
    sources: list[PreviewSource]


class PreviewSide(BaseModel):
    """一侧（当前 / 预演）的完整展开结果。"""

    recipe_name: str
    leaf_count: int
    paths: list[PreviewLeaf]
    allergen_summary: list[PreviewAllergenSummaryItem]


class AddedAllergen(BaseModel):
    """预演后新增的过敏原：预演中的全部来源。"""

    allergen: str
    after_sources: list[PreviewSource]


class RemovedAllergen(BaseModel):
    """预演后消失的过敏原：当前的全部来源。"""

    allergen: str
    before_sources: list[PreviewSource]


class ChangedAllergen(BaseModel):
    """仍存在但来源发生变化的过敏原。

    source_key 取「来源原料 + 位置路径」：
    - 两侧完全一致的来源计入 unchanged_sources（含数量、单位变化）；
    - 仅当前侧有的来源计入 removed_sources（原料被删 / 路径被改）；
    - 仅预演侧有的来源计入 added_sources（新增原料 / 路径变化后的来源）。
    """

    allergen: str
    added_sources: list[PreviewSource]
    removed_sources: list[PreviewSource]
    unchanged_sources: list[PreviewSource]


class LabelPreviewOut(BaseModel):
    recipe_id: str
    current: PreviewSide
    preview: PreviewSide
    added_allergens: list[AddedAllergen]
    removed_allergens: list[RemovedAllergen]
    changed_allergens: list[ChangedAllergen]
    # 过敏原名单本身发生增减 → 标签上的过敏原文字必然要改，必须重印
    allergen_list_changed: bool
    # 过敏原名单未变、但有来源原料或路径变化 → 文字可不变，是否重印由工坊按溯源需要判断
    sources_changed: bool
    reprint_recommended: bool
