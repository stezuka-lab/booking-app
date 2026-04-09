param(
  [Parameter(Mandatory = $true)]
  [string]$BackupFile
)

$ErrorActionPreference = "Stop"

function Convert-ToSyncDbUrl([string]$url) {
  if ([string]::IsNullOrWhiteSpace($url)) { return $url }
  if ($url.StartsWith("postgresql+asyncpg://")) {
    return "postgresql://" + $url.Substring("postgresql+asyncpg://".Length)
  }
  return $url
}

function Find-PgRestore {
  $cmd = Get-Command pg_restore -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $candidates = @(
    "C:\\Program Files\\PostgreSQL\\16\\bin\\pg_restore.exe",
    "C:\\Program Files\\PostgreSQL\\15\\bin\\pg_restore.exe",
    "C:\\Program Files\\PostgreSQL\\14\\bin\\pg_restore.exe"
  )
  foreach ($path in $candidates) {
    if (Test-Path $path) { return $path }
  }
  throw "pg_restore が見つかりません。PostgreSQL client tools をインストールしてください。"
}

$dbUrl = Convert-ToSyncDbUrl $env:DATABASE_URL
if ([string]::IsNullOrWhiteSpace($dbUrl)) {
  throw "DATABASE_URL が設定されていません。"
}
if (-not (Test-Path $BackupFile)) {
  throw ("バックアップファイルが見つかりません: {0}" -f $BackupFile)
}

$pgRestore = Find-PgRestore
& $pgRestore --clean --if-exists --no-owner --no-privileges --dbname $dbUrl $BackupFile

Write-Host ("Restore completed: {0}" -f (Resolve-Path $BackupFile))
