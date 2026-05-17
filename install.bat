@echo off
:: =============================================================================
:: Manga Downloader – Windows Installer
:: =============================================================================
:: How to run:
::   Double-click install.bat
::   OR right-click → "Run as administrator" if you get permission errors
:: =============================================================================

title Manga Downloader – Installer
color 0B

echo.
echo  +==========================================+
echo  ^|     Manga Downloader  --  Installer     ^|
echo  +==========================================+
echo.

:: ── Step 1: Check Python ─────────────────────────────────────────────────────
echo [.] Checking for Python 3.11 or newer...

python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [X] Python was not found on this computer.
    echo.
    echo  Please install Python 3.12 from:
    echo    https://www.python.org/downloads/
    echo.
    echo  IMPORTANT: During installation, tick the box that says
    echo  "Add Python to PATH" before clicking Install.
    echo.
    echo  After installing Python, run this installer again.
    echo.
    pause
    exit /b 1
)

:: Check version is 3.11+
python -c "import sys; exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [X] Your Python version is too old. Python 3.11 or newer is required.
    echo.
    echo  Download the latest version from:
    echo    https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do set PY_VER=%%i
echo  [OK] Found %PY_VER%

:: ── Step 2: Create virtual environment ───────────────────────────────────────
echo.
echo [.] Setting up virtual environment...

if exist .venv\ (
    echo  [!] Virtual environment already exists - skipping creation.
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo  [X] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo  [OK] Virtual environment created.
)

:: ── Step 3: Install Python dependencies ──────────────────────────────────────
echo.
echo [.] Installing Python packages (playwright, flask, requests)...

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet

if errorlevel 1 (
    echo  [X] Failed to install packages. Check your internet connection.
    pause
    exit /b 1
)
echo  [OK] Python packages installed.

:: ── Step 4: Install Chromium ──────────────────────────────────────────────────
echo.
echo [.] Installing Chromium browser for Playwright (this may take a minute)...

playwright install chromium

if errorlevel 1 (
    echo  [X] Failed to install Chromium.
    pause
    exit /b 1
)
echo  [OK] Chromium installed.

:: ── Step 5: Create launcher ───────────────────────────────────────────────────
echo.
echo [.] Creating launcher...

set LAUNCHER=Start Manga Downloader.bat
(
echo @echo off
echo title Manga Downloader
echo cd /d "%%~dp0"
echo call .venv\Scripts\activate.bat
echo echo.
echo echo   Starting Manga Downloader...
echo echo   Open your browser at: http://localhost:7337
echo echo   Close this window to stop.
echo echo.
echo start "" "http://localhost:7337"
echo python app.py
echo pause
) > "%LAUNCHER%"

echo  [OK] Launcher created: "%LAUNCHER%"

:: ── Done ──────────────────────────────────────────────────────────────────────
echo.
echo  +==========================================+
echo  ^|        Installation complete!           ^|
echo  +==========================================+
echo.
echo  To start the app:
echo    Double-click "Start Manga Downloader.bat"
echo.
echo  The app will open automatically in your browser at:
echo    http://localhost:7337
echo.
pause
