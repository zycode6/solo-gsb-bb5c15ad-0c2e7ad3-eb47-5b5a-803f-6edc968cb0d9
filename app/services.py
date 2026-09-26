"""业务逻辑：登记原料、保存配方（校验 + 原子写入）、展开配方。

保存配方的规则：
1. 所有引用的原料 / 子配方必须已存在，否则整体拒绝；
2. 不允许直接或间接循环引用（配方经任意链路引用回自身），否则整体拒绝；
3. 校验全部通过后，删除旧条目 + 写入新条目在同一事务中提交，
   任何失败都不会改动数据库中原有的配方。
"""
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Ingredient, Recipe, RecipeEntry, SeqCounter, utcnow
from app.schemas import RecipeIn


class ApiError(Exception):
    """业务校验失败，由全局异常处理器转成 JSON 响应。"""

    def __init__(self, status_code: int, code: str, message: str, extra: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}
        super().__init__(message)


# ---------- 编号 ----------

def next_code(db: Session, prefix: str, width: int) -> str:
    counter = db.get(SeqCounter, prefix)
    if counter is None:
        counter = SeqCounter(name=prefix, value=0)
        db.add(counter)
    counter.value += 1
    db.flush()
    return f"{prefix}-{counter.value:0{width}d}"


# ---------- 原料 ----------

def create_ingredient(db: Session, name: str, allergens: list[str]) -> Ingredient:
    existing = db.scalar(select(Ingredient).where(Ingredient.name == name))
    if existing is not None:
        raise ApiError(409, "DUPLICATE_NAME", f"原料名称已存在：{name}",
                       {"ingredient_id": existing.id})
    ingredient = Ingredient(id=next_code(db, "ING", 4), name=name, allergens=allergens)
    db.add(ingredient)
    db.commit()
    db.refresh(ingredient)
    return ingredient


# ---------- 配方保存 ----------

def _validate_references(db: Session, payload: RecipeIn) -> None:
    """所有引用必须存在，否则拒绝。"""
    ingredient_refs = sorted({e.ref_id for e in payload.entries if e.ref_type == "ingredient"})
    recipe_refs = sorted({e.ref_id for e in payload.entries if e.ref_type == "recipe"})

    missing: list[dict] = []
    if ingredient_refs:
        found = set(db.scalars(
            select(Ingredient.id).where(Ingredient.id.in_(ingredient_refs))))
        missing += [{"ref_type": "ingredient", "ref_id": r}
                    for r in ingredient_refs if r not in found]
    if recipe_refs:
        found = set(db.scalars(
            select(Recipe.id).where(Recipe.id.in_(recipe_refs))))
        missing += [{"ref_type": "recipe", "ref_id": r}
                    for r in recipe_refs if r not in found]

    if missing:
        raise ApiError(400, "UNKNOWN_REFERENCE",
                       "配方引用了不存在的原料或子配方", {"missing": missing})


def _find_cycle(adjacency: dict[str, list[str]], start: str) -> list[str] | None:
    """在配方引用图中寻找从 start 出发又回到 start 的路径，返回该路径或 None。"""
    visited = {start}

    def dfs(node: str, trail: list[str]) -> list[str] | None:
        for child in adjacency.get(node, []):
            if child == start:
                return trail + [child]
            if child not in visited:
                visited.add(child)
                found = dfs(child, trail + [child])
                if found:
                    return found
        return None

    return dfs(start, [start])


def _check_no_cycle(db: Session, recipe_id: str, payload: RecipeIn) -> None:
    """以「新条目替换旧条目」后的图检测是否出现回到 recipe_id 的环。"""
    adjacency: dict[str, list[str]] = defaultdict(list)
    rows = db.execute(
        select(RecipeEntry.recipe_id, RecipeEntry.ref_id)
        .where(RecipeEntry.ref_type == "recipe")
    ).all()
    for parent_id, child_id in rows:
        if parent_id != recipe_id:  # 本配方的旧组成将被整体替换，不参与
            adjacency[parent_id].append(child_id)
    adjacency[recipe_id] = [e.ref_id for e in payload.entries if e.ref_type == "recipe"]

    cycle = _find_cycle(adjacency, recipe_id)
    if cycle:
        raise ApiError(409, "CYCLE_DETECTED",
                       "配方存在直接或间接循环引用：" + " -> ".join(cycle),
                       {"cycle": cycle})


def save_recipe(db: Session, payload: RecipeIn, recipe_id: str | None = None) -> Recipe:
    """新建（recipe_id=None）或整体替换一个配方。失败时不改动任何已有数据。"""
    # ---- 全部校验先行，不做任何写入 ----
    if recipe_id is None:
        clash = db.scalar(select(Recipe).where(Recipe.name == payload.name))
        if clash is not None:
            raise ApiError(409, "DUPLICATE_NAME", f"配方名称已存在：{payload.name}",
                           {"recipe_id": clash.id})
    else:
        recipe = db.get(Recipe, recipe_id)
        if recipe is None:
            raise ApiError(404, "RECIPE_NOT_FOUND", f"配方不存在：{recipe_id}")
        clash = db.scalar(select(Recipe).where(
            Recipe.name == payload.name, Recipe.id != recipe_id))
        if clash is not None:
            raise ApiError(409, "DUPLICATE_NAME", f"配方名称已存在：{payload.name}",
                           {"recipe_id": clash.id})

    _validate_references(db, payload)
    if recipe_id is not None:
        # 新建配方尚无编号，不可能被任何现有配方引用，也就无法成环，无需检测
        _check_no_cycle(db, recipe_id, payload)

    # ---- 校验通过，同一事务内完成写入 ----
    if recipe_id is None:
        recipe = Recipe(id=next_code(db, "REC", 4),
                        name=payload.name, description=payload.description)
        db.add(recipe)
        db.flush()
    else:
        recipe.name = payload.name
        recipe.description = payload.description
        recipe.updated_at = utcnow()
        for old in list(recipe.entries):
            db.delete(old)
        db.flush()

    for position, entry in enumerate(payload.entries, start=1):
        db.add(RecipeEntry(
            id=next_code(db, "ENT", 6),
            recipe_id=recipe.id,
            position=position,
            ref_type=entry.ref_type,
            ref_id=entry.ref_id,
            quantity=entry.quantity,
            unit=entry.unit,
        ))
    db.commit()
    db.refresh(recipe)
    return recipe


# ---------- 查询 ----------

def get_recipe_or_404(db: Session, recipe_id: str) -> Recipe:
    recipe = db.get(Recipe, recipe_id)
    if recipe is None:
        raise ApiError(404, "RECIPE_NOT_FOUND", f"配方不存在：{recipe_id}")
    return recipe


def recipe_to_out(db: Session, recipe: Recipe) -> dict:
    """配方详情：条目按行号排序并解析引用名称。"""
    names = _load_names(db)
    return {
        "id": recipe.id,
        "name": recipe.name,
        "description": recipe.description,
        "created_at": recipe.created_at,
        "updated_at": recipe.updated_at,
        "entries": [
            {
                "id": e.id,
                "position": e.position,
                "ref_type": e.ref_type,
                "ref_id": e.ref_id,
                "ref_name": names.get((e.ref_type, e.ref_id), e.ref_id),
                "quantity": e.quantity,
                "unit": e.unit,
            }
            for e in sorted(recipe.entries, key=lambda x: x.position)
        ],
    }


def _load_names(db: Session) -> dict[tuple[str, str], str]:
    names: dict[tuple[str, str], str] = {}
    for row in db.scalars(select(Ingredient)):
        names[("ingredient", row.id)] = row.name
    for row in db.scalars(select(Recipe)):
        names[("recipe", row.id)] = row.name
    return names


# ---------- 配方展开 ----------

def expand_recipe(db: Session, recipe_id: str) -> dict:
    """展开配方的全部引用路径，并按过敏原汇总每条来源路径。

    同一子配方被使用多次时，每次使用都有各自的条目编号，
    因此会产生多条相互独立的路径，全部保留。
    """
    root = get_recipe_or_404(db, recipe_id)

    recipes = {r.id: r for r in db.scalars(select(Recipe))}
    ingredients = {i.id: i for i in db.scalars(select(Ingredient))}
    entries_by_recipe: dict[str, list[RecipeEntry]] = defaultdict(list)
    for entry in db.scalars(select(RecipeEntry)):
        entries_by_recipe[entry.recipe_id].append(entry)
    for entries in entries_by_recipe.values():
        entries.sort(key=lambda e: e.position)

    def step_of(entry: RecipeEntry) -> dict:
        parent = recipes[entry.recipe_id]
        if entry.ref_type == "ingredient":
            ref_name = ingredients[entry.ref_id].name
        else:
            ref_name = recipes[entry.ref_id].name
        return {
            "entry_id": entry.id,
            "position": entry.position,
            "recipe_id": parent.id,
            "recipe_name": parent.name,
            "ref_type": entry.ref_type,
            "ref_id": entry.ref_id,
            "ref_name": ref_name,
            "quantity": entry.quantity,
            "unit": entry.unit,
        }

    paths: list[dict] = []

    def walk(rid: str, prefix: list[dict], ancestors: frozenset[str]) -> None:
        for entry in entries_by_recipe.get(rid, []):
            step = step_of(entry)
            trail = prefix + [step]
            if entry.ref_type == "ingredient":
                ingredient = ingredients[entry.ref_id]
                paths.append({
                    "path": [s["entry_id"] for s in trail],
                    "steps": trail,
                    "ingredient_id": ingredient.id,
                    "ingredient_name": ingredient.name,
                    "allergens": list(ingredient.allergens),
                })
            else:
                # 保存时已保证无环；此处仍防御性截断，避免脏数据导致死循环
                if entry.ref_id in ancestors:
                    continue
                walk(entry.ref_id, trail, ancestors | {entry.ref_id})

    walk(root.id, [], frozenset({root.id}))

    # 按过敏原汇总：每个过敏原 -> 带来源路径的列表
    summary: dict[str, list[dict]] = defaultdict(list)
    for leaf in paths:
        for allergen in leaf["allergens"]:
            summary[allergen].append({
                "path": leaf["path"],
                "ingredient_id": leaf["ingredient_id"],
                "ingredient_name": leaf["ingredient_name"],
            })

    return {
        "recipe_id": root.id,
        "recipe_name": root.name,
        "leaf_count": len(paths),
        "paths": paths,
        "allergen_summary": [
            {"allergen": allergen, "sources": summary[allergen]}
            for allergen in sorted(summary)
        ],
    }
