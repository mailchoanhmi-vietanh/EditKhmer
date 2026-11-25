@echo off
chcp 65001 >nul
setlocal

echo ========================================================
echo   EditKhmer - Dong goi thanh 1 file EditKhmer.exe
echo ========================================================

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [LOI] Khong tim thay Python tren may build. Hay cai Python 3.11+ tu python.org
    pause
    exit /b 1
)

if not exist ".build_venv" (
    echo [1/3] Dang tao moi truong build tam...
    python -m venv .build_venv
)

call .build_venv\Scripts\activate.bat

echo [2/3] Dang cai dat thu vien dong goi...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt pyinstaller

echo [3/3] Dang build dist\EditKhmer.exe (khong cua so CMD)...
pyinstaller --clean --noconfirm VocalForge.spec

if %errorlevel% equ 0 (
    echo.
    echo ========================================================
    echo   THANH CONG! File exe nam tai: dist\EditKhmer.exe
    echo   Ban co the gui truc tiep file nay cho nguoi khac mo.
    echo ========================================================
) else (
    echo [LOI] Build that bai!
)

pause
