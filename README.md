# オンライン予約管理（booking-app）

空き枠・予約受付・Google カレンダー連携・管理画面です。**Kintone の解約検知 AI は別アプリ** [`../churn-insight-app/`](../churn-insight-app/) です。

**初めて起動する方は [`START.md`](./START.md) を開いてください。**

## セットアップ

1. Python 3.12+ を用意する。
2. `booking-app` で `pip install -r requirements.txt`
3. `.env.example` を `.env` にコピーし、`PUBLIC_BASE_URL`・`BOOKING_ADMIN_SECRET` などを設定。
4. 本番公開時は `.env.production.example` と `docs/DEPLOYMENT.md` を参照。
5. 本番 DB は `Postgres` を推奨します。`asyncpg` は依存関係に追加済みです。
6. 無料サーバー系は `python -m app.serve` で起動できます。`PORT` を自動で拾います。
7. 顧客情報を扱う本番環境では `BOOKING_DATA_ENCRYPTION_KEY` を設定してください。Google 連携トークンの暗号化保存に使います。
8. DB 変更履歴は `Alembic` で管理できます。手順は [`docs/MIGRATIONS.md`](./docs/MIGRATIONS.md) を参照してください。

## 起動

```bash
cd booking-app
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

- **Windows**: `run_dev.bat` / `run_server.bat`
- **Free PaaS**: `Procfile` / `render.yaml` / `python -m app.serve`
- **Vercel**: `index.py` + `vercel.json` + `python build.py`
- **Docker**: `docker compose up --build`
- **テスト**: `python -m pytest -q`
- **Migration**: `alembic upgrade head`

## API（抜粋）

| メソッド | パス | 説明 |
|---------|------|------|
| GET | `/health` | ヘルスチェック |
| GET | `/api/booking/oauth/google/status` | Google OAuth 設定状況 |
| GET | `/app` | Web ホーム |

`GET /docs` で OpenAPI 一覧。
