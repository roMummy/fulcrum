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

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8280"]
