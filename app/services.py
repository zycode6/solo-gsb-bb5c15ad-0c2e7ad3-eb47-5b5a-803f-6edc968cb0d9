"""业务逻辑：登记原料、保存配方（校验 + 原子写入）、展开配方。

保存配方的规则：
1. 所有引用的原料 / 子配方必须已存在，否则整体拒绝；
2. 不允许直接或间接循环引用（配方经任意链路引用回自身），否则整体拒绝；
3. 校验全部通过后，删除旧条目 + 写入新条目在同一事务中提交，
   任何失败都不会改动数据库中原有的配方。
"""
from collections import Counter, defaultdict

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


def validate_recipe_payload(db: Session, payload: RecipeIn,
                            recipe_id: str | None = None) -> None:
    """保存 / 预演共用的全部校验：名称冲突、引用存在性、循环引用。

    只读数据库，不做任何写入。recipe_id 为 None 表示新建（不做存在性与循环校验）。
    """
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


def save_recipe(db: Session, payload: RecipeIn, recipe_id: str | None = None) -> Recipe:
    """新建（recipe_id=None）或整体替换一个配方。失败时不改动任何已有数据。"""
    # ---- 全部校验先行，不做任何写入 ----
    validate_recipe_payload(db, payload, recipe_id)

    # ---- 校验通过，同一事务内完成写入 ----
    if recipe_id is None:
        recipe = Recipe(id=next_code(db, "REC", 4),
                        name=payload.name, description=payload.description)
        db.add(recipe)
        db.flush()
    else:
        recipe = db.get(Recipe, recipe_id)
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


# ---------- 标签预演：整体替换不落库，比较过敏原变化 ----------

def _load_graph(db: Session) -> tuple[
        dict[str, Recipe], dict[str, Ingredient], dict[str, list]]:
    """读取全部配方、原料，以及按配方归集、按行号排序的现有条目（统一为字典视图）。"""
    recipes = {r.id: r for r in db.scalars(select(Recipe))}
    ingredients = {i.id: i for i in db.scalars(select(Ingredient))}
    entries_by_recipe: dict[str, list] = defaultdict(list)
    for entry in db.scalars(select(RecipeEntry)):
        entries_by_recipe[entry.recipe_id].append({
            "entry_id": entry.id,
            "recipe_id": entry.recipe_id,
            "position": entry.position,
            "ref_type": entry.ref_type,
            "ref_id": entry.ref_id,
            "quantity": entry.quantity,
            "unit": entry.unit,
        })
    for entries in entries_by_recipe.values():
        entries.sort(key=lambda e: e["position"])
    return recipes, ingredients, entries_by_recipe


def _virtual_entries(payload: RecipeIn) -> list[dict]:
    """把预演请求中的根配方行转成统一的条目视图。

    尚未落库，所以没有条目编号（entry_id=None）；position 取行号。
    """
    return [
        {
            "entry_id": None,
            "position": position,
            "ref_type": e.ref_type,
            "ref_id": e.ref_id,
            "quantity": e.quantity,
            "unit": e.unit,
        }
        for position, e in enumerate(payload.entries, start=1)
    ]


def _expand_by_position(recipes: dict[str, Recipe],
                        ingredients: dict[str, Ingredient],
                        entries_by_recipe: dict[str, list],
                        root_id: str, root_name: str,
                        root_entries: list) -> dict:
    """以「每层条目位置」为路径标识展开整棵引用树。

    同一子配方被引用多次时，各引用在父配方中占据不同行号，
    因而形成不同的位置路径，重复引用全部保留为各自独立的来源。
    """

    def entries_of(recipe_id: str) -> list:
        if recipe_id == root_id:
            return root_entries
        return entries_by_recipe.get(recipe_id, [])

    def step_of(recipe_id: str, entry) -> dict:
        parent_name = root_name if recipe_id == root_id else recipes[recipe_id].name
        ref_type = entry["ref_type"]
        ref_id = entry["ref_id"]
        if ref_type == "ingredient":
            ref_name = ingredients[ref_id].name
        else:
            ref_name = recipes[ref_id].name
        return {
            "entry_id": entry.get("entry_id"),
            "position": entry["position"],
            "recipe_id": recipe_id,
            "recipe_name": parent_name,
            "ref_type": ref_type,
            "ref_id": ref_id,
            "ref_name": ref_name,
            "quantity": entry.get("quantity"),
            "unit": entry.get("unit"),
        }

    paths: list[dict] = []

    def walk(rid: str, prefix: list[dict], positions: list[int],
             ancestors: frozenset[str]) -> None:
        for entry in entries_of(rid):
            step = step_of(rid, entry)
            trail = prefix + [step]
            pos_path = positions + [entry["position"]]
            if step["ref_type"] == "ingredient":
                ingredient = ingredients[step["ref_id"]]
                paths.append({
                    "path": pos_path,
                    "steps": trail,
                    "ingredient_id": ingredient.id,
                    "ingredient_name": ingredient.name,
                    "allergens": list(ingredient.allergens),
                })
            else:
                # 保存时已保证无环；预演侧的循环也在校验阶段拒绝。此处仍防御性截断
                if step["ref_id"] in ancestors:
                    continue
                walk(step["ref_id"], trail, pos_path, ancestors | {step["ref_id"]})

    walk(root_id, [], [], frozenset({root_id}))

    summary: dict[str, list[dict]] = defaultdict(list)
    for leaf in paths:
        source = {k: leaf[k] for k in ("path", "steps", "ingredient_id", "ingredient_name")}
        for allergen in leaf["allergens"]:
            summary[allergen].append(source)

    return {
        "recipe_name": root_name,
        "leaf_count": len(paths),
        "paths": paths,
        "allergen_summary": [
            {"allergen": allergen, "sources": summary[allergen]}
            for allergen in sorted(summary)
        ],
    }


def _source_key(source: dict) -> tuple:
    """来源标识：来源原料 + 整条引用路径。

    对外展示的 path 以每层条目位置组成，重复引用因行号不同而成为不同来源；
    比较当前与预演两侧时，同一位置路径上的条目内容可能已被替换，
    因此标识中再带上每一步所在配方与引用对象（数量 / 单位变化不影响标识）。
    """
    chain = tuple(
        (step["recipe_id"], step["position"], step["ref_id"])
        for step in source["steps"]
    )
    return source["ingredient_id"], chain


def _source_out(source: dict) -> dict:
    return {
        "path": source["path"],
        "steps": source["steps"],
        "ingredient_id": source["ingredient_id"],
        "ingredient_name": source["ingredient_name"],
    }


def _diff_allergens(before: dict, after: dict) -> dict:
    """比较两侧展开结果，给出过敏原的新增 / 移除 / 来源变化。"""
    before_map = {item["allergen"]: item["sources"]
                  for item in before["allergen_summary"]}
    after_map = {item["allergen"]: item["sources"]
                 for item in after["allergen_summary"]}

    added_allergens: list[dict] = []
    removed_allergens: list[dict] = []
    changed_allergens: list[dict] = []

    for allergen in sorted(set(before_map) | set(after_map)):
        old_sources = before_map.get(allergen, [])
        new_sources = after_map.get(allergen, [])
        if not old_sources:
            added_allergens.append({
                "allergen": allergen,
                "after_sources": [_source_out(s) for s in new_sources],
            })
            continue
        if not new_sources:
            removed_allergens.append({
                "allergen": allergen,
                "before_sources": [_source_out(s) for s in old_sources],
            })
            continue

        # 多重集按来源原料 + 位置路径配对：未配对的即原料或路径发生了变化
        unmatched_old = Counter(_source_key(s) for s in old_sources)
        kept_new: list[dict] = []
        added: list[dict] = []
        for source in new_sources:
            key = _source_key(source)
            if unmatched_old.get(key, 0) > 0:
                unmatched_old[key] -= 1
                kept_new.append(source)
            else:
                added.append(source)
        removed: list[dict] = []
        for source in old_sources:
            key = _source_key(source)
            if unmatched_old.get(key, 0) > 0:
                unmatched_old[key] -= 1
                removed.append(source)
        if added or removed:
            changed_allergens.append({
                "allergen": allergen,
                "added_sources": [_source_out(s) for s in added],
                "removed_sources": [_source_out(s) for s in removed],
                "unchanged_sources": [_source_out(s) for s in kept_new],
            })

    list_changed = bool(added_allergens or removed_allergens)
    sources_changed = bool(changed_allergens)
    return {
        "added_allergens": added_allergens,
        "removed_allergens": removed_allergens,
        "changed_allergens": changed_allergens,
        "allergen_list_changed": list_changed,
        "sources_changed": sources_changed,
        "reprint_recommended": list_changed,
    }


def preview_label(db: Session, recipe_id: str, payload: RecipeIn) -> dict:
    """以整体替换后的组成预演标签变化，不写入数据库。

    校验与保存接口完全一致（复用同一套引用 / 循环 / 名称冲突校验）；
    随后分别展开当前配方与预演配方，路径以每层条目位置标识，
    比较过敏原的新增、移除与来源变化。
    """
    validate_recipe_payload(db, payload, recipe_id)

    recipes, ingredients, entries_by_recipe = _load_graph(db)
    root = recipes[recipe_id]

    current = _expand_by_position(
        recipes, ingredients, entries_by_recipe,
        root.id, root.name, entries_by_recipe.get(root.id, []))
    preview = _expand_by_position(
        recipes, ingredients, entries_by_recipe,
        root.id, payload.name, _virtual_entries(payload))

    diff = _diff_allergens(current, preview)
    return {
        "recipe_id": root.id,
        "current": current,
        "preview": preview,
        **diff,
    }
