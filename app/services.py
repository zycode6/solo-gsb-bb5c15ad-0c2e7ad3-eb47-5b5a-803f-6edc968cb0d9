"""业务逻辑：登记原料、保存配方（校验 + 原子写入）、展开配方、预演标签变化。

保存配方的规则：
1. 所有引用的原料 / 子配方必须已存在，否则整体拒绝；
2. 不允许直接或间接循环引用（配方经任意链路引用回自身），否则整体拒绝；
3. 校验全部通过后，删除旧条目 + 写入新条目在同一事务中提交，
   任何失败都不会改动数据库中原有的配方。

标签预演（label-preview）套用与保存完全相同的校验，但只做展开与对比，
不写入任何数据。
"""
from collections import defaultdict
from types import SimpleNamespace

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


def _validate_recipe_payload(db: Session, payload: RecipeIn,
                             recipe_id: str | None) -> Recipe | None:
    """保存与标签预演共用的全部校验（只读，不写入）。

    返回被替换的现有配方；新建（recipe_id=None）时返回 None。
    """
    if recipe_id is None:
        clash = db.scalar(select(Recipe).where(Recipe.name == payload.name))
        if clash is not None:
            raise ApiError(409, "DUPLICATE_NAME", f"配方名称已存在：{payload.name}",
                           {"recipe_id": clash.id})
        recipe = None
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
    return recipe


def save_recipe(db: Session, payload: RecipeIn, recipe_id: str | None = None) -> Recipe:
    """新建（recipe_id=None）或整体替换一个配方。失败时不改动任何已有数据。"""
    # ---- 全部校验先行，不做任何写入 ----
    recipe = _validate_recipe_payload(db, payload, recipe_id)

    # ---- 校验通过，同一事务内完成写入 ----
    if recipe is None:
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

def _load_graph(db: Session):
    """加载全部配方、原料与条目（条目转为 dict 并按行号排序），供展开使用。"""
    recipes = {r.id: r for r in db.scalars(select(Recipe))}
    ingredients = {i.id: i for i in db.scalars(select(Ingredient))}
    entries_by_recipe: dict[str, list[dict]] = defaultdict(list)
    for e in db.scalars(select(RecipeEntry)):
        entries_by_recipe[e.recipe_id].append({
            "entry_id": e.id,
            "position": e.position,
            "recipe_id": e.recipe_id,
            "ref_type": e.ref_type,
            "ref_id": e.ref_id,
            "quantity": e.quantity,
            "unit": e.unit,
        })
    for entries in entries_by_recipe.values():
        entries.sort(key=lambda x: x["position"])
    return recipes, ingredients, entries_by_recipe


def _expand_paths(root_id: str, root_entries: list[dict],
                  entries_by_recipe: dict[str, list[dict]],
                  recipes: dict, ingredients: dict) -> list[dict]:
    """从根配方的条目出发，展开全部直达原料叶子的路径。

    同一子配方被使用多次时，每次使用都有各自的条目（位置），
    因此会产生多条相互独立的路径，全部保留。
    """
    leaves: list[dict] = []

    def walk(entries: list[dict], prefix: list[dict], ancestors: frozenset[str]) -> None:
        for entry in entries:
            parent = recipes[entry["recipe_id"]]
            if entry["ref_type"] == "ingredient":
                ref_name = ingredients[entry["ref_id"]].name
            else:
                ref_name = recipes[entry["ref_id"]].name
            step = {
                "entry_id": entry["entry_id"],
                "position": entry["position"],
                "recipe_id": parent.id,
                "recipe_name": parent.name,
                "ref_type": entry["ref_type"],
                "ref_id": entry["ref_id"],
                "ref_name": ref_name,
                "quantity": entry["quantity"],
                "unit": entry["unit"],
            }
            trail = prefix + [step]
            if entry["ref_type"] == "ingredient":
                ingredient = ingredients[entry["ref_id"]]
                leaves.append({
                    "steps": trail,
                    "ingredient_id": ingredient.id,
                    "ingredient_name": ingredient.name,
                    "allergens": list(ingredient.allergens),
                })
            else:
                # 保存时已保证无环；此处仍防御性截断，避免脏数据导致死循环
                if entry["ref_id"] in ancestors:
                    continue
                walk(entries_by_recipe.get(entry["ref_id"], []),
                     trail, ancestors | {entry["ref_id"]})

    walk(root_entries, [], frozenset({root_id}))
    return leaves


def expand_recipe(db: Session, recipe_id: str) -> dict:
    """展开配方的全部引用路径，并按过敏原汇总每条来源路径。"""
    root = get_recipe_or_404(db, recipe_id)
    recipes, ingredients, entries_by_recipe = _load_graph(db)
    leaves = _expand_paths(root.id, entries_by_recipe.get(root.id, []),
                           entries_by_recipe, recipes, ingredients)

    # 对外路径标识使用条目编号（ENT-xxxxxx）
    paths = [
        {
            "path": [s["entry_id"] for s in leaf["steps"]],
            "steps": leaf["steps"],
            "ingredient_id": leaf["ingredient_id"],
            "ingredient_name": leaf["ingredient_name"],
            "allergens": leaf["allergens"],
        }
        for leaf in leaves
    ]

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


# ---------- 标签变化预演 ----------

def _allergen_sources(leaves: list[dict]) -> dict[str, dict]:
    """过敏原 -> {(原料编号, 位置路径): 原料名称}。

    路径标识由每层条目位置组成；重复引用因所在条目位置不同，
    天然成为不同来源，全部保留。
    """
    sources: dict[str, dict] = defaultdict(dict)
    for leaf in leaves:
        path = tuple(step["position"] for step in leaf["steps"])
        for allergen in leaf["allergens"]:
            sources[allergen][(leaf["ingredient_id"], path)] = leaf["ingredient_name"]
    return sources


def _source_list(sources: dict) -> list[dict]:
    """来源按路径排序输出，便于人工逐项核对。"""
    return [
        {"ingredient_id": ingredient_id, "ingredient_name": name, "path": list(path)}
        for (ingredient_id, path), name in sorted(
            sources.items(), key=lambda item: (item[0][1], item[0][0]))
    ]


def preview_label_change(db: Session, recipe_id: str, payload: RecipeIn) -> dict:
    """预演「整体替换」后的过敏原标签变化，不写入任何数据。

    校验规则与保存接口完全一致；分别展开当前配方与预演配方，
    按过敏原对比来源（原料 + 每层条目位置组成的路径），
    给出新增、移除、以及来源原料或路径发生变化的过敏原。
    """
    recipe = get_recipe_or_404(db, recipe_id)
    _validate_recipe_payload(db, payload, recipe_id)  # 与保存相同的校验，失败即报同样的错

    recipes, ingredients, entries_by_recipe = _load_graph(db)

    # 当前配方：条目与位置均取自数据库
    current = _expand_paths(recipe_id, entries_by_recipe.get(recipe_id, []),
                            entries_by_recipe, recipes, ingredients)

    # 预演配方：根条目来自请求体（位置从 1 起，与保存时一致），子配方仍取数据库
    preview_entries = [
        {
            "entry_id": None,  # 未保存，尚无条目编号
            "position": position,
            "recipe_id": recipe_id,
            "ref_type": e.ref_type,
            "ref_id": e.ref_id,
            "quantity": e.quantity,
            "unit": e.unit,
        }
        for position, e in enumerate(payload.entries, start=1)
    ]
    preview_recipes = dict(recipes)
    preview_recipes[recipe_id] = SimpleNamespace(id=recipe_id, name=payload.name)
    preview = _expand_paths(recipe_id, preview_entries, entries_by_recipe,
                            preview_recipes, ingredients)

    before = _allergen_sources(current)
    after = _allergen_sources(preview)

    added, removed, changed = [], [], []
    for allergen in sorted(set(before) | set(after)):
        b, a = before.get(allergen, {}), after.get(allergen, {})
        item = {"allergen": allergen,
                "before": _source_list(b), "after": _source_list(a)}
        if not b:
            added.append(item)
        elif not a:
            removed.append(item)
        elif set(b) != set(a):  # 仍存在，但来源原料或路径变了
            changed.append(item)

    return {
        "recipe_id": recipe_id,
        "recipe_name": recipe.name,
        "preview_name": payload.name,
        "added_allergens": added,
        "removed_allergens": removed,
        "changed_allergens": changed,
    }
