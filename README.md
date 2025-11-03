# redmine_dify_monitor

個人学習用に作成したサンプルツールです。Codex により作成されています。
Redmine のチケット更新を監視し、Dify ワークフローで審査結果を解析して Microsoft Teams に通知するモニタリングツールです。  

## 主な機能
- Redmine の最新チケットを定期ポーリングし、更新のあったものだけを解析
- Dify ワークフロー経由で審査テキスト・ステータスを取得し、Adaptive Card 形式で Teams へ通知
- `caseid_mismatch` を検知した場合、通常の却下通知より強いアラートを送信
- 処理済みチケットは SQLite (`processed_issues.db`) に保存し、二重処理を防止
- ログレベルを環境変数で切り替え可能、ローテーション付きファイル＋標準出力に出力

## セットアップ手順

### 1. リポジトリを取得

```bash
git clone <REPO_URL>
cd redmine_dify_monitor
```

### 2. 環境変数ファイルを準備

```bash
cp .env.example .env
```

`.env` を編集し、環境に合わせて値を設定してください。

| 変数名 | 説明 | 例 |
| ------ | ---- | --- |
| `REDMINE_URL` | Redmine のベース URL | `http://redmine.example.com` |
| `REDMINE_API_KEY` | Redmine API キー | `xxxxxxxxxxxxxxxx` |
| `DIFY_API_URL` | Dify ワークフロー実行エンドポイント | `http://dify:5001/v1/workflows/execute` |
| `DIFY_API_KEY` | Dify API キー | `yyyyyyyyyyyyyyyy` |
| `TEAMS_WEBHOOK_URL` | 通常通知用 Teams Webhook | `https://graph.microsoft.com/...` |
| `TEAMS_WEBHOOK_SECONDARY_URL` | 却下・重大アラート時の追加通知先 (任意) | `https://graph.microsoft.com/...` |
| `POLL_INTERVAL` | ポーリング間隔（秒） | `60` |
| `LOG_LEVEL` | ログレベル (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`) | `INFO` |

### 3. 永続化ディレクトリを作成

```bash
mkdir -p logs state
```

Docker コンテナから `/var/log/redmine_dify_monitor`（ログ）と `/var/lib/redmine_dify_monitor`（SQLite DB）がそれぞれマウントされます。ホスト側の権限が不足する場合は `chown` / `chmod` で調整してください。

### 4. Docker Compose で起動

```bash
docker compose up -d
```

既存の外部ネットワーク `docker_default` に接続する構成です。ネットワーク名が異なる場合は `docker-compose.yml` を編集してください。

## ログと状態管理
- ログは `/var/log/redmine_dify_monitor/redmine_dify_monitor.log` に出力され、ローテーションしながら Docker 標準出力にも流れます。
- 処理済みチケットの更新時刻は `/var/lib/redmine_dify_monitor/processed_issues.db`（SQLite）に保存されます。ファイル破損時は削除で再生成できます。
- `LOG_LEVEL` を `DEBUG` に設定すると Dify リクエスト/レスポンスや Adaptive Card の内容が詳細に記録されます。

## 通知とアラートの挙動
- `caseid_mismatch` を検知した場合、🚨 アイコン付きの高優先度カードを送信し、「異なる受付番号への回答」と明示して確認を促します。
- 通常の「却下」「承認」通知も Adaptive Card で配信され、二次通知先が設定されている場合は却下時のみ追加で送信します。
- Dify 応答が `status="ok"` 以外（`caseid_mismatch` を除く）の場合は通知を行わず、処理済みのみ記録します。

## 手動実行（開発用途）

Docker を使わずローカルで試す場合は Python 3.10 以上を用意し、`pip install -r requirements.txt` 後に以下を実行します。

```bash
export $(grep -v '^#' .env | xargs)  # 必要に応じて環境変数を設定
python3 redmine_dify_monitor.py
```

## トラブルシューティング
- **パーミッションエラーが出る**: `logs/` や `state/` の所有者/権限を確認し、コンテナ内ユーザーが書き込めるように調整してください。
- **Teams に通知が届かない**: Webhook URL、ネットワーク疎通、`LOG_LEVEL=DEBUG` 時の送信ログを確認してください。
- **Dify から `caseid_missing` が多発する**: `redmine_ticket_qa_parser.py` に `print()` デバッグを追加して回答冒頭に caseid が含まれているか確認してください。

## ライセンス

このリポジトリのライセンスは `LICENSE` ファイルを参照してください。
