# Windows counterpart of run.sh. Arguments are passed through to ArenaOnAir.
$ErrorActionPreference = 'Stop'
$RepoDir = $PSScriptRoot
$VenvDir = if ($env:ARENAONAIR_VENV) { $env:ARENAONAIR_VENV } else { Join-Path $HOME '.venvs\arenaonair' }
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
# Windows PowerShell's native argument handling strips embedded double quotes.
# Keep the Python string literals single-quoted so the probe survives it.
$Probe = "import importlib.util, sys; sys.exit(0 if sys.version_info >= (3, 11) and all(importlib.util.find_spec(m) for m in ('websockets', 'kokoro', 'numpy', 'sounddevice', 'PySide6', 'pip')) else 1)"

$Ready = $false
if (Test-Path -LiteralPath $VenvPython) {
    & $VenvPython -c $Probe *> $null
    $Ready = $LASTEXITCODE -eq 0
}
if (-not $Ready) {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Host 'ArenaOnAir needs uv to install Python, the window, and voices.'
        Write-Host 'Install uv: https://docs.astral.sh/uv/getting-started/installation/'
        Write-Host 'Then reopen PowerShell and rerun this command. Manual Python setup: docs/install.md'
        exit 1
    }
    if (-not (Test-Path -LiteralPath $VenvPython)) {
        & uv venv --python 3.12 $VenvDir
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    Write-Host 'Installing ArenaOnAir with its window and neural voices (first run can take several minutes)...'
    & uv pip install --python $VenvPython -e "$RepoDir[tts,ui]"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$PreviousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $RepoDir 'src'
    if ($PreviousPythonPath) { $env:PYTHONPATH += [IO.Path]::PathSeparator + $PreviousPythonPath }
    & $VenvPython -m arenaonair.app @args
    $AppExitCode = $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $PreviousPythonPath
}
exit $AppExitCode
