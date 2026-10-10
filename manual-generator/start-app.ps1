param(
    [string]$Python = "",
    [string]$DataDir = "",
    [ValidateRange(1, 65535)][int]$Port = 8765,
    [string[]]$RegisterProject = @(),
    [switch]$NoBrowser,
    [switch]$CheckOnly,
    [switch]$InstallDependencies
)

$ErrorActionPreference = "Stop"
$taskCallerDirectory = (Get-Location).Path
$taskLocalPython = Join-Path $PSScriptRoot ".venv/Scripts/python.exe"
$taskRequirements = Join-Path $PSScriptRoot "requirements.txt"

function Resolve-TaskExecutable([string]$TaskName) {
    if (Test-Path -LiteralPath $TaskName -PathType Leaf) {
        return (Resolve-Path -LiteralPath $TaskName).Path
    }
    $taskCommand = Get-Command -Name $TaskName -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -eq $taskCommand) { throw "找不到 Python：$TaskName" }
    return $taskCommand.Source
}

function Get-TaskPythonInfo([string]$TaskExecutable, [string[]]$TaskArguments = @()) {
    $taskProbe = 'import json,struct,sys; print(json.dumps(dict(major=sys.version_info.major,minor=sys.version_info.minor,bits=struct.calcsize(chr(80))*8,venv=sys.prefix!=sys.base_prefix,executable=sys.executable)))'
    $taskOutput = & $TaskExecutable @TaskArguments -c $taskProbe 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $taskOutput) { return $null }
    return ($taskOutput | ConvertFrom-Json)
}

function Assert-TaskPython($TaskInfo) {
    if ($null -eq $TaskInfo -or $TaskInfo.major -ne 3 -or $TaskInfo.minor -notin @(11, 12) -or $TaskInfo.bits -ne 64) {
        throw "需要 Python 3.11 或 3.12 的 64 位版；可用 -Python 指定已安装的解释器。"
    }
}

function New-TaskEnvironment([string]$TaskBasePython) {
    if (-not (Test-Path -LiteralPath $taskLocalPython -PathType Leaf)) {
        if ($CheckOnly) { throw "本地 .venv 尚未创建；正常启动会创建，或用 -Python 指定现有环境。" }
        Write-Host "正在应用目录创建 Python 环境……"
        & $TaskBasePython -m venv (Join-Path $PSScriptRoot ".venv")
        if ($LASTEXITCODE -ne 0) { throw "创建 Python 环境失败。" }
    }
    return $taskLocalPython
}

function Test-TaskDependencies([string]$TaskExecutable) {
    $taskDependencyProbe = @'
import importlib.metadata as m
import sys
try:
    import cadquery
    import OCP
    import fitz
    expected = {'cadquery': '2.7.0', 'cadquery-ocp': '7.8.1.1.post1', 'PyMuPDF': '1.26.6'}
    mismatched = [name for name, version in expected.items() if m.version(name) != version]
    sys.exit(3 if mismatched else 0)
except Exception:
    sys.exit(3)
'@
    & $TaskExecutable -c $taskDependencyProbe
    return ($LASTEXITCODE -eq 0)
}

if ($CheckOnly -and $InstallDependencies) { throw "-CheckOnly 只检查环境，不能同时安装依赖。" }
$taskRequiredFiles = @("backend/app_server.py", "frontend/index.html", "frontend/app.js", "frontend/viewer.js", "frontend/style.css", "frontend/navigation.css", "frontend/vendor/three.module.js", "frontend/vendor/three.core.js", "frontend/vendor/TrackballControls.js", "requirements.txt")
foreach ($taskRelativeFile in $taskRequiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $taskRelativeFile) -PathType Leaf)) {
        throw "应用文件不完整：$taskRelativeFile。请完整解压应用包。"
    }
}

if ($Python) {
    $taskPython = Resolve-TaskExecutable $Python
} elseif (Test-Path -LiteralPath $taskLocalPython -PathType Leaf) {
    $taskPython = $taskLocalPython
} else {
    $taskBaseInfo = $null
    $taskLauncher = Get-Command -Name "py.exe" -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $taskLauncher) {
        foreach ($taskVersion in @("-3.12", "-3.11")) {
            try { $taskBaseInfo = Get-TaskPythonInfo $taskLauncher.Source @($taskVersion) } catch { $taskBaseInfo = $null }
            if ($null -ne $taskBaseInfo -and $taskBaseInfo.bits -eq 64) { break }
        }
    }
    if ($null -eq $taskBaseInfo) {
        try { $taskBaseInfo = Get-TaskPythonInfo (Resolve-TaskExecutable "python.exe") } catch { $taskBaseInfo = $null }
    }
    Assert-TaskPython $taskBaseInfo
    $taskPython = New-TaskEnvironment $taskBaseInfo.executable
}

$taskInfo = Get-TaskPythonInfo $taskPython
Assert-TaskPython $taskInfo
$taskDependenciesReady = Test-TaskDependencies $taskPython
if (-not $taskDependenciesReady -or $InstallDependencies) {
    if ($CheckOnly) { throw "依赖缺失、版本不一致或无法导入；正常启动会安装 requirements.txt 中的版本。" }
    if (-not $taskInfo.venv) {
        # Keep global Python unchanged when a base interpreter was supplied.
        $taskPython = New-TaskEnvironment $taskInfo.executable
        $taskInfo = Get-TaskPythonInfo $taskPython
        Assert-TaskPython $taskInfo
    }
    Write-Host "首次准备或修复依赖；后续正常启动会复用当前环境……"
    & $taskPython -m pip install -r $taskRequirements
    if ($LASTEXITCODE -ne 0) { throw "依赖安装失败，请检查网络和上方输出后重试。" }
    if (-not (Test-TaskDependencies $taskPython)) { throw "依赖仍无法导入，请检查 Python 位数和安装结果。" }
}

$taskProjectPaths = @()
foreach ($taskProject in $RegisterProject) {
    $taskProjectPath = if ([IO.Path]::IsPathRooted($taskProject)) { [IO.Path]::GetFullPath($taskProject) } else { [IO.Path]::GetFullPath((Join-Path $taskCallerDirectory $taskProject)) }
    if (-not (Test-Path -LiteralPath (Join-Path $taskProjectPath "steps.json") -PathType Leaf)) {
        throw "注册已有项目需要该目录包含 steps.json：$taskProjectPath"
    }
    $taskProjectPaths += $taskProjectPath
}
if ($DataDir) {
    $taskDataPath = if ([IO.Path]::IsPathRooted($DataDir)) { [IO.Path]::GetFullPath($DataDir) } else { [IO.Path]::GetFullPath((Join-Path $taskCallerDirectory $DataDir)) }
} else {
    $taskDataPath = Join-Path $PSScriptRoot "app-data"
}

Write-Host "Python：$taskPython"
Write-Host "本地数据：$taskDataPath"
if ($CheckOnly) {
    Write-Host "检查通过：Python、依赖与应用文件可用；未启动服务、注册项目或生成插图。"
    exit 0
}

$taskPortProbe = New-Object Net.Sockets.TcpClient
try {
    $taskConnection = $taskPortProbe.BeginConnect("127.0.0.1", $Port, $null, $null)
    if ($taskConnection.AsyncWaitHandle.WaitOne(300)) {
        try { $taskPortProbe.EndConnect($taskConnection) } catch { }
        if ($taskPortProbe.Connected) { throw "端口 $Port 已被使用。请关闭原服务，或使用 -Port 指定其他端口。" }
    }
} finally { $taskPortProbe.Dispose() }

$taskUrl = "http://127.0.0.1:$Port/"
$taskServerArguments = @((Join-Path $PSScriptRoot "backend/app_server.py"), "--data-dir", $taskDataPath, "--port", "$Port")
foreach ($taskProjectPath in $taskProjectPaths) { $taskServerArguments += @("--register-project", $taskProjectPath) }
Write-Host "启动安装说明工作台：$taskUrl"
Write-Host "关闭服务请在此窗口按 Ctrl+C。启动本身不会重新生成项目插图或 PDF。"
$taskBrowserOpened = $false
Push-Location -LiteralPath $PSScriptRoot
try {
    & $taskPython -u @taskServerArguments | ForEach-Object {
        Write-Output $_
        if (-not $NoBrowser -and -not $taskBrowserOpened -and "$_" -eq "APPLICATION $taskUrl") {
            $taskBrowserOpened = $true
            try { Start-Process -FilePath $taskUrl } catch { Write-Host "请手动打开：$taskUrl" }
        }
    }
    $taskServerExitCode = $LASTEXITCODE
    if ($taskServerExitCode -ne 0) { throw "工作台退出，返回码 $taskServerExitCode。请检查上方信息。" }
} finally { Pop-Location }
