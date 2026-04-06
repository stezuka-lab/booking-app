param(
    [Parameter(Mandatory = $true)]
    [string]$Root
)
$Root = $Root.TrimEnd('\', '/')
if (-not (Test-Path (Join-Path $Root 'run_dev.bat'))) {
    Write-Error "run_dev.bat not found in: $Root"
    exit 1
}
$startup = [Environment]::GetFolderPath('Startup')
$lnkPath = Join-Path $startup 'AItest Reservation Server.lnk'
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnkPath)
$sc.TargetPath = Join-Path $Root 'run_dev.bat'
$sc.WorkingDirectory = $Root
$sc.Description = 'Local FastAPI (uvicorn) for AItest'
$sc.WindowStyle = 7
$sc.Save()
Write-Host "Created: $lnkPath"
Write-Host "To disable: delete this shortcut or run uninstall_autostart.bat"
