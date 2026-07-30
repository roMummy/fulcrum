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

# 主服务默认端口统一为 8180，和本地 Windows 保留端口段错开。
EXPOSE 8180

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8180"]
