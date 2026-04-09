param(
  [string]$OutDir = ".\\backups"
)

$ErrorActionPreference = "Stop"

function Convert-ToSyncDbUrl([string]$url) {
  if ([string]::IsNullOrWhiteSpace($url)) { return $url }
  if ($url.StartsWith("postgresql+asyncpg://")) {
    return "postgresql://" + $url.Substring("postgresql+asyncpg://".Length)
  }
  return $url
}

function Find-PgDump {
  $cmd = Get-Command pg_dump -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $candidates = @(
    "C:\\Program Files\\PostgreSQL\\16\\bin\\pg_dump.exe",
    "C:\\Program Files\\PostgreSQL\\15\\bin\\pg_dump.exe",
    "C:\\Program Files\\PostgreSQL\\14\\bin\\pg_dump.exe"
  )
  foreach ($path in $candidates) {
    if (Test-Path $path) { return $path }
  }
  throw "pg_dump が見つかりません。PostgreSQL client tools をインストールしてください。"
}

$dbUrl = Convert-ToSyncDbUrl $env:DATABASE_URL
if ([string]::IsNullOrWhiteSpace($dbUrl)) {
  throw "DATABASE_URL が設定されていません。"
}

$pgDump = Find-PgDump
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$outFile = Join-Path $OutDir ("booking_{0}.dump" -f $stamp)

& $pgDump --format=custom --no-owner --no-privileges --file $outFile $dbUrl

Write-Host ("Backup created: {0}" -f (Resolve-Path $outFile))
