FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    DB_PATH=/data/census.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 本地持久化存储目录（SQLite 数据库文件），由 Compose 挂载命名卷
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
