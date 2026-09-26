"""配方保存与查询（含标签核对用的展开视图）。"""
from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import services
from app.database import get_db
from app.models import Recipe, RecipeEntry
from app.schemas import ExpansionOut, RecipeIn, RecipeOut, RecipeSummary

router = APIRouter(prefix="/recipes", tags=["配方"])


@router.post("", response_model=RecipeOut, status_code=status.HTTP_201_CREATED,
             summary="新建配方（引用不存在或校验失败时不写入任何数据）")
def create_recipe(payload: RecipeIn, db: Session = Depends(get_db)):
    recipe = services.save_recipe(db, payload)
    return services.recipe_to_out(db, recipe)


@router.get("", response_model=list[RecipeSummary], summary="列出全部配方")
def list_recipes(db: Session = Depends(get_db)):
    counts = dict(db.execute(
        select(RecipeEntry.recipe_id, func.count())
        .group_by(RecipeEntry.recipe_id)
    ).all())
    return [
        {
            "id": r.id,
            "name": r.name,
            "entry_count": counts.get(r.id, 0),
            "updated_at": r.updated_at,
        }
        for r in db.scalars(select(Recipe).order_by(Recipe.id))
    ]


@router.get("/{recipe_id}", response_model=RecipeOut, summary="查询配方及其条目")
def get_recipe(recipe_id: str, db: Session = Depends(get_db)):
    recipe = services.get_recipe_or_404(db, recipe_id)
    return services.recipe_to_out(db, recipe)


@router.put("/{recipe_id}", response_model=RecipeOut,
            summary="整体替换配方（引用不存在或出现循环时，原配方保持不变）")
def replace_recipe(recipe_id: str, payload: RecipeIn, db: Session = Depends(get_db)):
    recipe = services.save_recipe(db, payload, recipe_id=recipe_id)
    return services.recipe_to_out(db, recipe)


@router.get("/{recipe_id}/expansion", response_model=ExpansionOut,
            summary="展开全部引用路径并按过敏原汇总来源（标签核对）")
def get_expansion(recipe_id: str, db: Session = Depends(get_db)):
    return services.expand_recipe(db, recipe_id)
