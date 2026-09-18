@echo off
REM Builds dist\slgking.exe - one self-contained file, no Python needed.
REM ASCII only on purpose: cmd.exe runs .bat files through cp936 here and
REM non-ASCII comments come out mangled. Same reason the exe name is ASCII
REM while the window title is Chinese.
setlocal
cd /d "%~dp0"

where python >nul 2>nul || (echo Python not found on PATH & exit /b 1)

python -c "import PyInstaller" >nul 2>nul || (
    echo Installing PyInstaller...
    python -m pip install --upgrade pyinstaller || exit /b 1
)

REM customtkinter ships themes/*.json and fonts/*.otf as package data. The
REM hooks-contrib hook runs collect_data_files and picks those up, so the spec
REM needs no datas entry. Verified with --smoke, see README.
python -c "import customtkinter" >nul 2>nul || (
    echo Installing customtkinter...
    python -m pip install customtkinter || exit /b 1
)

REM Built from the spec, not from slg_main.py on the command line: passing the
REM script and flags here makes PyInstaller REWRITE slgking.spec, silently
REM discarding any hand edit.
REM
REM The spec builds windowless (console=False) so double-clicking never opens a
REM black window. The CLI flags still print: slg_main._attach_console borrows
REM the terminal they were launched from and leaves a double-click alone.
python -m PyInstaller --noconfirm --clean slgking.spec || exit /b 1

echo.
echo Built: %CD%\dist\slgking.exe
endlocal
