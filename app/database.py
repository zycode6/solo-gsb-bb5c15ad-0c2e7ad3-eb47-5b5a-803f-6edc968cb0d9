"""数据库连接与会话管理。

默认使用项目目录下的 SQLite 文件，可通过环境变量 DATABASE_URL 覆盖，例如：
    DATABASE_URL=sqlite:////data/labels.db
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./data/labels.db")


def _ensure_sqlite_dir(url: str) -> None:
    """SQLite 文件所在目录不存在时自动创建，保证数据库初始化随启动完成。"""
    prefix = "sqlite:///"
    if url.startswith(prefix):
        path = url[len(prefix):]
        if path and path != ":memory:":
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)


_ensure_sqlite_dir(DATABASE_URL)

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


def get_db():
    """FastAPI 依赖：每个请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表（幂等），应用启动时调用。"""
    from app import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(engine)
