@echo off
setlocal enabledelayedexpansion

:: Get current folder as absolute path
set "SCRIPT_DIR=%~dp0"
:: Remove trailing backslash
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo ==============================================
echo Phone Auto Backup - Windows Startup Setup
echo ==============================================
echo.

echo 1. Creating hidden runner script (run_hidden.vbs)...
(
echo Set WshShell = CreateObject^("WScript.Shell"^)
echo WshShell.Run Chr^(34^) ^& "%SCRIPT_DIR%\start.bat" ^& Chr^(34^), 0, False
) > "%SCRIPT_DIR%\run_hidden.vbs"
echo Done.
echo.

echo 2. Registering shortcut in Windows Startup directory...
powershell -Command "$WshShell = New-Object -ComObject WScript.Shell; $Shortcut = $WshShell.CreateShortcut(\"$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\PhoneAutoBackup.lnk\"); $Shortcut.TargetPath = \"wscript.exe\"; $Shortcut.Arguments = \"`\"%SCRIPT_DIR%\run_hidden.vbs`\"\"; $Shortcut.WorkingDirectory = \"%SCRIPT_DIR%\"; $Shortcut.Save()"
echo Done.
echo.

echo ==============================================
echo Installation Complete!
echo Phone Auto Backup will now start silently in
echo the background when Windows boots up.
echo ==============================================
echo.
pause
