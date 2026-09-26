"""测试环境：使用独立的临时 SQLite 数据库。"""
import os
import tempfile

# 必须在导入 app 之前设置，保证引擎指向临时库
_tmp_db = tempfile.NamedTemporaryFile(prefix="label-test-", suffix=".db", delete=False)
_tmp_db.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp_db.name}"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    """每个用例前后都重建空库，互不影响。"""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
