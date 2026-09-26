FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 依赖随镜像构建安装
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# SQLite 数据目录（可用 named volume 挂载持久化）
RUN mkdir -p /data
ENV DATABASE_URL=sqlite:////data/labels.db

EXPOSE 8000

# 数据库初始化在应用启动时自动完成（见 app/main.py lifespan）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
