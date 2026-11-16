# SPDX-License-Identifier: MIT
<#
.SYNOPSIS
  构建发布包（只构建一次，压缩两次）。

.DESCRIPTION
  流程：
    1. 在系统临时目录准备一份「代码包」内容（源码 + exe）
    2. 压缩 → bilimusic_v<版本>_win-x64-lite.zip
    3. 往临时目录的 bin\ 里补 ffmpeg.exe / ffprobe.exe
    4. 再压缩 → bilimusic_v<版本>_win-x64.zip
    5. 删掉临时目录

  两个包内容完全相同，唯一区别是后者多了两个依赖文件。
  输出的 zip 直接放在项目根目录，不保留任何中间文件夹。

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File build_release.ps1
  powershell -ExecutionPolicy Bypass -File build_release.ps1 -Version 0.1.0
#>
param([string]$Version = "0.1.0")

$ErrorActionPreference = 'Stop'
$root = 'D:\AI\B站下载歌切'
$pub  = Join-Path $root 'bilimusic'

$zipLite = Join-Path $root "bilimusic_v${Version}_win-x64-lite.zip"
$zipFull = Join-Path $root "bilimusic_v${Version}_win-x64.zip"

# 临时工作目录（放在系统临时目录，构建完删除）
$stage = Join-Path ([IO.Path]::GetTempPath()) "bilimusic-build-$(Get-Random)"
$bin = Join-Path $stage 'bin'
$spec = Join-Path $root 'build_exe.spec'

function Write-Step($n, $total, $msg) {
    Write-Host "===== [$n/$total] $msg =====" -ForegroundColor Cyan
}

try {
    # ---------------------------------------------------------------
    Write-Step 1 6 "准备临时目录"
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    New-Item -ItemType Directory -Path $bin -Force | Out-Null
    Write-Host "      $stage"

    # ---------------------------------------------------------------
    Write-Step 2 6 "复制源码"
    $skipDirs = @('.git', '__pycache__', 'docs', '.github', 'dist', 'downloads', 'workspace', 'workspace_e2e', 'tests', '_旧版脚本_已并入core')
    # ⚠️ 敏感文件绝不进发布包：cookies.txt（真实登录凭证）、user.json（私有配置）、
    #    manual_labels.json 的人工标注含私人判断、日志与缓存
    $skipFiles = @('cookies.txt', 'bilimusic.exe', '_dl_failed.json', '_dl_run.log',
                   '_dl_status.txt', 'bilimusic.log')
    Get-ChildItem -LiteralPath $pub -Force |
      Where-Object { $_.Name -notin $skipDirs -and $_.Name -notin $skipFiles } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $stage -Recurse -Force
    }
    # 私有配置目录：只带 defaults.json 与 user.example.json，不带 user.json
    $cfgStage = Join-Path $stage 'config'
    if (Test-Path -LiteralPath $cfgStage) {
        Remove-Item -LiteralPath (Join-Path $cfgStage 'user.json') -Force -ErrorAction SilentlyContinue
    }
    # 人工标注：发布包里给空白模板，不带用户真实标注
    $ml = Join-Path $stage 'manual_labels.json'
    if (Test-Path -LiteralPath $ml) {
        Copy-Item -LiteralPath (Join-Path $pub 'manual_labels.example.json') -Destination $ml -Force
    }
    # 兜底：清掉任何残留 cookies / 日志 / 缓存
    Get-ChildItem -LiteralPath $stage -Recurse -File -Force -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -in $skipFiles -or $_.Name -like '*.log' -or $_.Name -like 'cookies*' -and $_.Extension -ne '.example' } |
      Remove-Item -Force -ErrorAction SilentlyContinue
    # 递归清掉所有层级的 __pycache__（Copy-Item -Recurse 会把子目录的也带进来）
    Get-ChildItem -LiteralPath $stage -Recurse -Directory -Force -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -eq '__pycache__' } |
      Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    # 开发用脚本不进发布包
    foreach ($dev in @('gui\shot.ps1', 'gui\make_icon.py')) {
        Remove-Item -LiteralPath (Join-Path $stage $dev) -Force -ErrorAction SilentlyContinue
    }
    Copy-Item -LiteralPath (Join-Path $root 'README.md') -Destination (Join-Path $stage 'README.md') -Force

    # 只带用户需要的文档
    $docOut = Join-Path $stage 'docs'
    New-Item -ItemType Directory -Path $docOut -Force | Out-Null
    foreach ($d in @('GUI.md', 'CONFIG.md', 'RULES.md', 'PITFALLS.md', 'API.md')) {
        $src = Join-Path $pub "docs\$d"
        if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $docOut -Force }
    }
    foreach ($f in @('LICENSE')) {
        $src = Join-Path $pub $f
        if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $stage -Force }
    }
    # 代码包说明（后面会换成完整包说明）
    $liteDoc = Join-Path $pub 'docs\README-LITE.md'
    if (Test-Path -LiteralPath $liteDoc) {
        Copy-Item -LiteralPath $liteDoc -Destination (Join-Path $stage '使用说明.md') -Force
    }
    $bat = "@echo off`r`nchcp 65001 >nul`r`ncd /d `"%~dp0`"`r`nstart `"`" `"bilimusic.exe`"`r`n"
    [IO.File]::WriteAllText((Join-Path $stage 'start.bat'), $bat, (New-Object Text.UTF8Encoding $false))

    # ---------------------------------------------------------------
    Write-Step 3 6 "打包 bilimusic.exe"
    $log = Join-Path $stage '_pyinstaller.log'
    $env:BILI_SRC = $pub
    $pyArgs = @('-m', 'PyInstaller', '--noconfirm', '--clean',
                '--distpath', $stage, '--workpath', (Join-Path $stage '_build'), $spec)
    $proc = Start-Process -FilePath 'python' -ArgumentList $pyArgs -NoNewWindow -Wait `
        -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.err"
    Remove-Item Env:\BILI_SRC -ErrorAction SilentlyContinue
    $exe = Join-Path $stage 'bilimusic.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        Write-Host "--- stderr ---" -ForegroundColor Red
        Get-Content "$log.err" -Tail 40 -ErrorAction SilentlyContinue
        throw "PyInstaller 未生成 exe（见 $log.err）"
    }
    Get-ChildItem $stage -Directory -Filter '_build' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force
    Get-ChildItem $stage -Force | Where-Object { $_.Name -like '_pyinstaller*' } | Remove-Item -Force
    Write-Host "      bilimusic.exe $([math]::Round((Get-Item $exe).Length/1MB,1)) MB"

    # ---------------------------------------------------------------
    Write-Step 4 6 "压缩【代码包】"
    if (Test-Path -LiteralPath $zipLite) { Remove-Item -LiteralPath $zipLite -Force }
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zipLite -CompressionLevel Optimal
    Write-Host ("      {0}  ({1} MB)" -f (Split-Path $zipLite -Leaf),
        [math]::Round((Get-Item $zipLite).Length/1MB,1))

    # ---------------------------------------------------------------
    Write-Step 5 6 "补入依赖，压缩【完整包】"
    $ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if ($ffmpeg) {
        Copy-Item -LiteralPath $ffmpeg.Source -Destination (Join-Path $bin 'ffmpeg.exe') -Force
        Write-Host "      ffmpeg.exe $([math]::Round((Get-Item (Join-Path $bin 'ffmpeg.exe')).Length/1MB,1)) MB"
    } else {
        Write-Host "      (未找到 ffmpeg，完整包将不含它)" -ForegroundColor Yellow
    }
    $ffprobe = Get-Command ffprobe -ErrorAction SilentlyContinue
    if ($ffprobe) {
        Copy-Item -LiteralPath $ffprobe.Source -Destination (Join-Path $bin 'ffprobe.exe') -Force
        Write-Host "      ffprobe.exe $([math]::Round((Get-Item (Join-Path $bin 'ffprobe.exe')).Length/1MB,1)) MB"
    }

    # 换成完整包说明
    $fullDoc = Join-Path $pub 'docs\README-FULL.md'
    if (Test-Path -LiteralPath $fullDoc) {
        Copy-Item -LiteralPath $fullDoc -Destination (Join-Path $stage '使用说明.md') -Force
    }

    if (Test-Path -LiteralPath $zipFull) { Remove-Item -LiteralPath $zipFull -Force }
    Compress-Archive -Path (Join-Path $stage '*') -DestinationPath $zipFull -CompressionLevel Optimal
    Write-Host ("      {0}  ({1} MB)" -f (Split-Path $zipFull -Leaf),
        [math]::Round((Get-Item $zipFull).Length/1MB,1))

    # ---------------------------------------------------------------
    Write-Step 6 6 "清理临时文件"
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "      已删除临时目录"

    # 把 exe 也放一份到源码目录，这样才能直接双击运行
    $pubExe = Join-Path $pub 'bilimusic.exe'
    $tmpExe = Join-Path ([IO.Path]::GetTempPath()) "bilimusic-exe-$(Get-Random).exe"
    try {
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $z = [IO.Compression.ZipFile]::OpenRead($zipFull)
        $entry = $z.Entries | Where-Object { $_.FullName -eq 'bilimusic.exe' }
        if ($entry) {
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $tmpExe, $true)
            Copy-Item -LiteralPath $tmpExe -Destination $pubExe -Force
            Write-Host "      已放置 bilimusic.exe 到源码目录"
        }
        $z.Dispose()
    }
    catch { Write-Host "      (跳过放置 exe: $_)" -ForegroundColor DarkGray }
    finally { Remove-Item $tmpExe -Force -ErrorAction SilentlyContinue }
}
finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "构建完成，产物在 $root ：" -ForegroundColor Green
Get-ChildItem $root -Filter 'bilimusic_*.zip' | ForEach-Object {
    Write-Host ("  {0}  ({1} MB)" -f $_.Name, [math]::Round($_.Length/1MB,1))
}
