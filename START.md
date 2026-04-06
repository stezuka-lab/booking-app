# booking-app — 起動ガイド

**オンライン予約**（空き枠・Google カレンダー・予約リンクごとの専用 URL）。**Python 3.11+** が必要です。

- 解約検知 AI → [`../churn-insight-app/`](../churn-insight-app/)
- パーソナルシート → [`../personal-sheet-app/`](../personal-sheet-app/)

## 最短で試す（ローカル）

| 手順 | 内容 |
|------|------|
| 1 | `.env.example` をコピーして `.env` にする |
| 2 | `PUBLIC_BASE_URL=http://127.0.0.1:8000` のまま、`BOOKING_ADMIN_SECRET` は例の `dev-demo-secret` で可（本番は長いランダム値へ） |
| 3 | `run_dev.bat` を実行するか、下記コマンドを **`booking-app` 内**で実行 |
| 4 | メール再送・リマインドを使うなら別ウィンドウで `run_jobs.bat` を実行 |

```text
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

ジョブ専用ランナー:

```text
python -m app.booking.job_runner loop --interval 60
```

- **画面**: `http://127.0.0.1:8000/app`
- **ヘルス**: `http://127.0.0.1:8000/health`（`status` が `ok` なら起動成功）
- **止め方**: ターミナルを閉じるか **Ctrl + C**

`127.0.0.1` はこの PC のみです。LAN から使う場合は `run_server.bat` を使ってください。起動時にこの PC の LAN IP を自動検出し、`PUBLIC_BASE_URL` と `GOOGLE_OAUTH_REDIRECT_URI` を `http://<LAN_IP>:8000` に上書きして待ち受けます。必要なら `allow_lan_firewall.bat` を管理者で実行して Windows ファイアウォールの受信許可を追加してください。  
`run_dev.bat` / `run_server.bat` は埋め込み scheduler を無効にして起動するため、確認メールの再送・リマインド・掘り起こしを使う場合は `run_jobs.bat` を別で起動してください。

## 同じネットワーク内の他PCから開く

1. `run_server.bat` を起動
2. 画面に表示された `http://<LAN_IP>:8000/app` を確認
3. 必要なら `allow_lan_firewall.bat` を管理者で 1 回実行
4. 他の PC からその URL を開く

Google カレンダー連携も同じ LAN URL を使います。Google Cloud Console の OAuth 設定には、起動時に表示された URL のオリジンと callback を登録してください。

## 依存のインストール（手動）

`run_dev.bat` は起動時に `pip install` します。手動なら:

```text
python -m pip install -r requirements.txt
```

## Windows で起動を自動化

**`install_autostart.bat`** でスタートアップ登録（**`uninstall_autostart.bat`** で解除）。二重起動するとポート `8000` が競合します。

## Google カレンダー（`GOOGLE_OAUTH_*`）

1. [Google Cloud Console → 認証情報](https://console.cloud.google.com/apis/credentials) で OAuth クライアント（ウェブ）を作成
2. **承認済みリダイレクト URI** に `.env` の **`GOOGLE_OAUTH_REDIRECT_URI`** と**完全一致**で登録（例: `http://127.0.0.1:8000/api/booking/oauth/google/callback`）
3. **`GOOGLE_OAUTH_CLIENT_ID`** / **`GOOGLE_OAUTH_CLIENT_SECRET`** / **`GOOGLE_OAUTH_REDIRECT_URI`** を `.env` に記入し再起動
4. **`/app/calendar`** または **`/app/settings`** の担当表で「**Google で連携**」（予約リンクは **`/app/campaigns`**）

状態確認: **`GET /api/booking/oauth/google/status`** または `/health` の `booking.google_oauth_ready`。詳細は [`README.md`](./README.md)。

## 主な設定はどこで行うか

| 設定内容 | 場所 | 補足 |
|----------|------|------|
| **予約可能時間**（開始・終了）・**土日祝** | ブラウザ **`/app/settings`** → **「予約可能時間・休日」**で保存 | 組織共通設定 |
| **担当の割当**（均等／優先度など）・**自動確定** | **`/app/settings`** の **「割当・確定」** | `routing_mode`, `auto_confirm` |
| **担当の追加・Zoom URL・スキルタグ・Google 連携** | **`/app/settings`** の **「Google カレンダー連携」**表（または **`/app/calendar`**） | `POST .../staff`, `PATCH /api/booking/admin/staff/{id}` |
| **Google OAuth（クライアント ID 等）** | **`.env`** の `GOOGLE_OAUTH_*` | 再起動が必要 |
| **予約リンクごとの前後余白・先行予約上限・予約区分** | **`/app/campaigns`** | リンク単位で設定 |
| **サーバー既定の予約バッファ（分）** | **`.env`** の `BOOKING_BUFFER_MINUTES`（既定 **0**） | リンク未設定時のみのフォールバック |
| **管理者 API 自体の有効化** | **`.env`** の `BOOKING_ADMIN_SECRET` | 空だと管理 API は無効 |
| **担当ごとの Google 表示名・メール** | **`/app/calendar`** または **`/app/settings`** の「Google で連携」 | 連携後に userinfo を保存 |

**予約リンク**（専用 URL の発行・編集・一覧）は **`/app/campaigns`**（従来の **`/app/admin`** と同一画面）です。

## 本番公開前の注意

- 本番用の設定例は [`./.env.production.example`](./.env.production.example)
- デプロイ手順は [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md)
- 本番 DB は `Postgres` 推奨です。`DATABASE_URL` は `postgresql+asyncpg://...` を使います
- 公開 URL で起動する場合:
  - `BOOKING_SESSION_SECRET` が空だと起動停止
  - `BOOKING_SEED_DEMO=true` だと起動停止

## その他

| 内容 | 参照 |
|------|------|
| Docker | フォルダ内 `Dockerfile` / `docker compose` |
| 自動テスト | `python -m pytest -q` |
| CI | リポジトリルートの `.github/workflows/ci.yml` |
| クラウドデプロイ | [`docs/DEPLOYMENT.md`](./docs/DEPLOYMENT.md) |

## トラブル

| 現象 | 対処 |
|------|------|
| `python` が見つからない | PATH に Python を追加、または `py` を試す |
| ポート使用中 | 8000 を他プロセスが使用中。変更するか終了する |
| 画面が真っ白 | `.env` と `pip install` を確認 |
| DB エラー（開発） | `data/app.db` を削除して再起動（**データ消失**） |
