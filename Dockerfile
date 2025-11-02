FROM python:3.11-slim

WORKDIR /app

# 必要パッケージをインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリをコピー（マウントもされるので必須ではないが保険）
COPY redmine_dify_monitor.py .

CMD ["python", "/app/redmine_dify_monitor.py"]