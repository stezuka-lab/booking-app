# DB Migration 運用

このアプリは、既存環境との互換のために **起動時の簡易スキーマ補正** をまだ残しています。  
今後の本番運用では、DB 変更は **Alembic** を正本にしてください。

## 目的

- DB 変更を履歴として残す
- サーバー移行時に同じ手順で更新できる
- 起動時補正への依存を減らす

## 事前準備

依存を入れます。

```bash
pip install -r requirements.txt
```

## 新規環境

新しい DB に対しては migration を実行します。

```bash
alembic upgrade head
```

## 既存環境

すでにアプリで作られた DB がある場合は、まずバックアップを取ってから現在スキーマを baseline として登録します。

```bash
alembic stamp 0001_baseline
```

その後、新しい migration が追加されたら以下で更新します。

```bash
alembic upgrade head
```

## 新しい migration の作成

モデル変更後に revision を作ります。

```bash
alembic revision -m "describe change"
```

必要に応じて `migrations/versions/` の生成ファイルを編集してください。

## 補足

- `sqlite+aiosqlite://...` は Alembic 実行時に `sqlite://...` へ変換します
- `postgresql+asyncpg://...` は Alembic 実行時に `postgresql+psycopg://...` へ変換します
- Render / Neon / 将来の移行先でも同じ migration 手順を使えます
