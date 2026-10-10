param(
    [string]$Step,
    [string]$Project = "projects/draft",
    [string]$Title = "安装说明草案",
    [string]$Template,
    [string]$Regions,
    [int]$Port = 8765
)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $taskPython)) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "请先安装 Python 3.12 64 位版。" }
    & $taskPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，请检查上方信息。" }
}
if (-not (Test-Path -LiteralPath (Join-Path $Project "steps.json"))) {
    if (-not $Step) { throw "首次运行请传入 -Step STP文件路径。" }
    & $taskPython scripts/manual.py draft --step $Step --project $Project --title $Title
    if ($LASTEXITCODE -ne 0) { throw "STP 草案生成失败，请检查上方信息。" }
    & $taskPython scripts/manual.py render --project $Project
    if ($LASTEXITCODE -ne 0) { throw "插图生成失败；草案已保留，可在预览中修改后重试。" }
}
if ($Template -or $Regions) {
    if (-not $Template -or -not $Regions) { throw "绑定原模板请同时传入 -Template PDF路径 和 -Regions 换图槽位配置路径。" }
    & $taskPython scripts/manual.py template --project $Project --template $Template --regions $Regions
    if ($LASTEXITCODE -ne 0) { throw "模板对应关系校验失败；原项目和原模板不会被覆盖。" }
}
& $taskPython scripts/manual.py review --project $Project --port $Port
if ($LASTEXITCODE -ne 0) { throw "预览启动失败，请检查上方信息。" }
