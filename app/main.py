"""应用入口：启动时完成数据库初始化。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.database import init_db
from app.routers import ingredients, recipes
from app.services import ApiError


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 数据库初始化随启动完成：自动建目录、建表（幂等）
    init_db()
    yield


app = FastAPI(
    title="食品工坊配方标签核查 API",
    description=(
        "供小型食品工坊登记原料与过敏原、以带独立条目编号的原料或子配方组成配方，"
        "并在查询时展开全部引用路径、按过敏原汇总来源，用于成品标签核对。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.message, **exc.extra}},
    )


@app.get("/", tags=["系统"], summary="服务信息")
def root():
    return {
        "service": "食品工坊配方标签核查 API",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", tags=["系统"], summary="健康检查")
def health():
    return {"status": "ok"}


app.include_router(ingredients.router)
app.include_router(recipes.router)
