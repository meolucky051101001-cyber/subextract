# Đăng ký / huỷ SubExtract tự khởi động khi đăng nhập Windows.
#
# Bật:  powershell -ExecutionPolicy Bypass -File .\autostart.ps1
# Tắt:  powershell -ExecutionPolicy Bypass -File .\autostart.ps1 -Remove
#
# Shortcut đặt trong thư mục Startup của user, gọi pythonw.exe trong .venv
# chạy launcher.py --no-browser. Launcher tự nhận diện server đã chạy nên bật
# trùng lặp không sinh thêm tiến trình và không mở trình duyệt.

param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root '.venv\Scripts\pythonw.exe'
$Launcher = Join-Path $Root 'launcher.py'
$Icon = Join-Path $Root 'assets\SubExtract.ico'
$Link = Join-Path ([Environment]::GetFolderPath('Startup')) 'SubExtract.lnk'

if ($Remove) {
    if (Test-Path $Link) {
        Remove-Item $Link
        Write-Output "Da xoa tu khoi dong: $Link"
    } else {
        Write-Output "Khong co shortcut tu khoi dong o: $Link"
    }
    exit 0
}

if (-not (Test-Path $Python)) { Write-Error "Thieu $Python - tao .venv truoc (xem run.bat)"; exit 1 }
if (-not (Test-Path $Launcher)) { Write-Error "Thieu $Launcher"; exit 1 }

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($Link)
$shortcut.TargetPath = $Python
$shortcut.Arguments = "`"$Launcher`" --no-browser"
$shortcut.WorkingDirectory = $Root
$shortcut.WindowStyle = 7
if (Test-Path $Icon) { $shortcut.IconLocation = "$Icon,0" }
$shortcut.Description = 'SubExtract - may chu OCR local tai http://127.0.0.1:8787'
$shortcut.Save()

Write-Output "Da dang ky tu khoi dong: $Link"
Write-Output "  Target : $($shortcut.TargetPath)"
Write-Output "  Args   : $($shortcut.Arguments)"
Write-Output "  Workdir: $($shortcut.WorkingDirectory)"
Write-Output "Huy bang: powershell -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`" -Remove"
