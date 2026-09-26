"""原料登记与查询。"""
from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import services
from app.database import get_db
from app.models import Ingredient
from app.schemas import IngredientIn, IngredientOut

router = APIRouter(prefix="/ingredients", tags=["原料"])


@router.post("", response_model=IngredientOut, status_code=status.HTTP_201_CREATED,
             summary="登记原料及其过敏原")
def create_ingredient(payload: IngredientIn, db: Session = Depends(get_db)):
    return services.create_ingredient(db, payload.name, payload.allergens)


@router.get("", response_model=list[IngredientOut], summary="列出全部原料")
def list_ingredients(db: Session = Depends(get_db)):
    return db.scalars(select(Ingredient).order_by(Ingredient.id)).all()


@router.get("/{ingredient_id}", response_model=IngredientOut, summary="查询单个原料")
def get_ingredient(ingredient_id: str, db: Session = Depends(get_db)):
    ingredient = db.get(Ingredient, ingredient_id)
    if ingredient is None:
        raise services.ApiError(404, "INGREDIENT_NOT_FOUND", f"原料不存在：{ingredient_id}")
    return ingredient
