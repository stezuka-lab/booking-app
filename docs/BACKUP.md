# バックアップ手順

このアプリでは、予約・顧客情報・監査ログは `DATABASE_URL` の Postgres を正本として扱います。  
サーバー移行前、環境変数変更前、本番メンテナンス前には必ずバックアップを取得してください。

## 1. 最低限の運用ルール

- 毎日 1 回: DB バックアップを取得
- 変更作業の直前: 手動バックアップを追加取得
- 保持世代:
  - 日次 7 世代
  - 週次 4 世代
- バックアップファイルは、アプリ本体とは別の保存先に置く
- 復元確認を月 1 回は行う

## 2. 事前準備

Windows では `pg_dump` / `pg_restore` が使える必要があります。

候補:
- PostgreSQL をインストール
- `pg_dump.exe`, `pg_restore.exe` が PATH に通っている状態にする

確認:

```powershell
pg_dump --version
pg_restore --version
```

## 3. バックアップ取得

PowerShell:

```powershell
cd C:\Users\z3088\OneDrive\Desktop\AItest\booking-app
powershell -ExecutionPolicy Bypass -File .\scripts\backup_postgres.ps1
```

出力先を変える場合:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\backup_postgres.ps1 -OutDir .\backups
```

成功すると `booking_YYYYMMDD_HHMMSS.dump` が作成されます。

## 4. 復元

注意: 復元は既存 DB を上書きする可能性があります。必ず別環境か、作業前の追加バックアップを取ってから実行してください。

```powershell
cd C:\Users\z3088\OneDrive\Desktop\AItest\booking-app
powershell -ExecutionPolicy Bypass -File .\scripts\restore_postgres.ps1 -BackupFile .\backups\booking_20260409_120000.dump
```

## 5. サーバー移行時の推奨順

1. 現行環境でバックアップ取得
2. 新環境へ環境変数を設定
3. 新環境の DB へ復元
4. Alembic か起動時補正でスキーマ整合を確認
5. `/health` を確認
6. 管理画面で `今すぐ整合性を回復` を実行
7. 予約ページで空き確認

## 6. バックアップ後の確認

- ファイルサイズが 0 ではない
- 直近の予約が含まれている
- 復元テスト環境で起動できる

## 7. 重要な注意

- `BOOKING_DATA_ENCRYPTION_KEY` を失うと、暗号化済みデータの復号ができません
- DB バックアップと一緒に、以下の秘密情報も安全に保管してください
  - `BOOKING_DATA_ENCRYPTION_KEY`
  - `BOOKING_SESSION_SECRET`
  - `GOOGLE_OAUTH_CLIENT_SECRET`
  - `SMTP_PASSWORD`
