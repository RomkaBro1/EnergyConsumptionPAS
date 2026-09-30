$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать виртуальное окружение' }
}
$dependencyHash = (Get-FileHash -LiteralPath 'requirements.txt' -Algorithm SHA256).Hash
$savedDependencyHash = if (Test-Path -LiteralPath '.venv\dependencies.sha256') { Get-Content -LiteralPath '.venv\dependencies.sha256' -Raw } else { '' }
if ($savedDependencyHash.Trim() -ne $dependencyHash) {
    & '.venv\Scripts\python.exe' -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости' }
    Set-Content -LiteralPath '.venv\dependencies.sha256' -Value $dependencyHash
}
& '.venv\Scripts\python.exe' run.py serve
