@echo off
rem ============================================================
rem  Tieba Album Downloader - one-click build (ASCII ONLY, safe)
rem  Double-click this file. Window STAYS OPEN at the end.
rem  Output EXE goes in the "dist" subfolder.
rem
rem  安全模式（兼容沙箱）：不 rm -rf、不用 --clean。
rem  PyInstaller 的 COLLECT 在 dist 已存在同名目录时会尝试删除它而
rem  被沙箱拦截，因此先把旧 dist 改名移走，再不带 --clean 直接打包。
rem ============================================================
cd /d "%~dp0"

set "STAMP=%TIME::=%"
set "STAMP=%STAMP: =0%"
set "STAMP=%STAMP:.=%"

echo.
echo ============================================================
echo   Tieba Album Downloader - BUILD
echo   Folder: %~dp0
echo ============================================================
echo.

echo [1/4] Checking Python...
python --version
if errorlevel 1 (
  echo   [WARN] "python" not found on PATH, trying "py -3"...
  py -3 --version
  set "PY=py -3"
) else (
  set "PY=python"
)

echo.
echo [2/4] Installing dependencies...
%PY% -m pip install --upgrade pip
%PY% -m pip install playwright lxml pyinstaller
%PY% -c "import playwright, lxml, PyInstaller; print('deps ok')"

echo.
echo [3/4] Moving old dist aside (safe, not deleting)...
if exist "dist" (
  if not exist "_old_builds" mkdir "_old_builds"
  move /Y "dist" "_old_builds\dist_%STAMP%" >nul 2>&1 && echo   moved dist -> _old_builds\dist_%STAMP%
)

echo.
echo [4/4] Building EXE (onedir, no --clean)...
%PY% -m PyInstaller tieba_album_getter.spec --noconfirm
%PY% -m PyInstaller tieba_album_getter_slim.spec --noconfirm

echo.
echo ============================================================
echo   BUILD STEPS FINISHED.
echo   If you saw ERROR / Traceback / not found above, copy that part.
echo   The exe is inside the  dist  subfolder in this folder.
echo   Share: zip the whole Chinese-named folder(s).
echo ============================================================
pause
