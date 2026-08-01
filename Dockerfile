FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# SaaS 容器使用独立 8280 端口，部署时不会占用旧项目的 8180。
EXPOSE 8280

# 容器健康状态使用 readiness 而不是仅检查端口。数据库 revision 落后或初始化
# 失败时进程仍可提供 /healthz 排障，但 Docker 必须把业务容器标记为 unhealthy。
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8280/readyz', timeout=3).read()"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8280"]
