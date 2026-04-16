# Deployment

このアプリは FastAPI + Postgres + 定期ジョブ構成を本番前提にするのが安全です。無料サーバーでのテスト運用もできるように、起動コマンドと公開 URL の自動判定を入れています。

- Vercel のようなフロント専用構成ではなく、Python プロセスを常駐できる先を使う
- Postgres が必要
- HTTPS の公開 URL を 1 つ決める

## 今回追加した無料サーバー向け対応

- `python -m app.serve`
  - `PORT` / `HOST` を拾って起動
- `Procfile`
  - Heroku 系 / Railway 系の標準起動に対応
- `render.yaml`
  - Render の Blueprint でそのまま読み込める
- `PUBLIC_BASE_URL` の自動補正
  - `RENDER_EXTERNAL_URL`
  - `RAILWAY_STATIC_URL`
  - `RAILWAY_PUBLIC_DOMAIN`
- `GOOGLE_OAUTH_REDIRECT_URI` の自動補正
  - 上の公開 URL に合わせて callback を自動導出

## 向いている無料枠

- Render Web Service
- Railway
- Fly.io
- Google Cloud Run

DB は `Postgres` を推奨します。`SQLite + ローカルファイル` は検証用途にとどめてください。

## こちらで実装済みの公開向け防御

- `TrustedHostMiddleware`
- セキュリティヘッダー
- HTTPS 配備時の `Secure` セッション Cookie
- 管理 API / 認証 API / 予約変更系への same-origin チェック
- ログイン試行レート制限
- 公開 URL での危険設定チェック
  - `BOOKING_SESSION_SECRET` 未設定なら起動停止
  - `BOOKING_SEED_DEMO=true` なら起動停止
- `/health` のデモ予約情報はローカル時のみ表示

## 本番用設定ファイル

`.env.production.example` をベースにしてください。

必須:

- `DATABASE_URL=postgresql+asyncpg://...`
- `BOOKING_ADMIN_SECRET=長いランダム文字列`
- `BOOKING_SESSION_SECRET=長いランダム文字列`
- `BOOKING_SEED_DEMO=false`

`PUBLIC_BASE_URL` は明示設定が最優先です。未設定または `127.0.0.1` のままでも、Render / Railway では自動検出できます。ただし Google OAuth を安定させるため、本番では明示設定を推奨します。

推奨:

- `SECURITY_TRUSTED_HOSTS=あなたのドメイン`
- `SECURITY_FORCE_HTTPS_REDIRECT=true`
- `API_DOCS_ENABLED=false`

## デプロイ手順

1. `.env.production.example` を見ながら、ホスティング先の環境変数へ値を登録する
2. Postgres を作成し、接続文字列を `DATABASE_URL` に設定する
3. アプリをデプロイする
4. 起動コマンドは `python -m app.serve`
5. Render なら `render.yaml` を使うか、`Start Command` に `python -m app.serve` を入れる
6. `https://あなたのURL/health` を開き、`status=ok` を確認する
7. `https://あなたのURL/app` を開き、ログイン画面が出ることを確認する
8. Google OAuth を使う場合は Google Cloud Console に本番リダイレクト URI を登録する

## Postgres のおすすめ

- Neon
- Supabase
- Render Postgres

最初の候補は `Neon` です。無料で始めやすく、接続文字列をそのまま `DATABASE_URL` に入れられます。

接続文字列の例:

```text
postgresql+asyncpg://postgres:your-password@ep-xxxxx.ap-northeast-1.aws.neon.tech/neondb?ssl=require
```

## Google OAuth

Google Cloud Console 側で次を一致させてください。

- 承認済みの JavaScript 生成元:
  - `https://あなたの本番URL`
- 承認済みのリダイレクト URI:
  - `https://あなたの本番URL/api/booking/oauth/google/callback`

`.env` 側:

- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_OAUTH_REDIRECT_URI`

## SMTP

メールを使うなら次を設定します。

- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_FROM`

## Vercel

This app can run on Vercel with the current reduced feature set, but only with an external Postgres database.

- `index.py` exports the FastAPI app for Vercel
- `vercel.json` sets the Python runtime and build command
- `build.py` copies `app/web/static/*` into `public/assets/web/`
- Set `VERCEL=true`
- Set `DATABASE_URL` to Postgres, not SQLite
- Set `PUBLIC_BASE_URL=https://<your-project>.vercel.app`
- Keep startup side effects off on Vercel unless you intentionally need them:
  - `STARTUP_INIT_DB=false`
  - `STARTUP_BOOTSTRAP_ADMIN=false`
  - `STARTUP_SEED_DEMO=false`
  - `STARTUP_EMBEDDED_JOBS=false`

Recommended Vercel stack:

- Runtime: Vercel Python
- DB: Neon Postgres
- Mail: external SMTP
- File uploads: disabled, or move to Blob/S3 if re-enabled later

## 起動確認

```bash
python -m pytest -q
```

公開前に最低限確認する項目:

- `/health` が 200
- `/app/login` が表示される
- ログインできる
- `/app/campaigns` で予約リンクを作成できる
- 予約完了メールが届く
- Google カレンダー連携できる
- キャンセル時に予定が消える
