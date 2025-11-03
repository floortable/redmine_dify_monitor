FROM python:3.11-slim

WORKDIR /app

# 必要パッケージをインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# デバッグ用途のsqlite3 CLIを追加
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# アプリをコピー（マウントもされるので必須ではないが保険）
COPY redmine_dify_monitor.py .
COPY state_manager.py .

CMD ["python", "/app/redmine_dify_monitor.py"]
