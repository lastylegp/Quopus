@echo off
setlocal enabledelayedexpansion
rem =====================================================================
rem build_sidwrapper_windows.bat
rem =====================================================================
rem Builds sidwrapper.dll on Windows via MSYS2 + MinGW64.
rem
rem What it does, in one shot:
rem   1. Detects MSYS2 (looks at common install paths + MSYS2_ROOT env).
rem   2. Updates pacman and installs the toolchain + libsidplayfp.
rem   3. Compiles sidwrapper.cpp -> sidwrapper.dll using g++ from MinGW64.
rem   4. Verifies sid_set_roms is exported.
rem   5. Copies the runtime DLLs sidwrapper.dll depends on
rem      (libsidplayfp-6.dll, libstdc++-6.dll, libgcc_s_seh-1.dll,
rem       libwinpthread-1.dll, libgcrypt-20.dll, libgpg-error-0.dll,
rem       libgomp-1.dll - whatever ldd reports) next to quopus.py.
rem
rem Usage:
rem   1. Install MSYS2 from https://www.msys2.org first if you don't
rem      have it. Default path C:\msys64 is auto-detected.
rem   2. Drop this .bat next to quopus.py + sidwrapper.cpp and run it.
rem      Run from a normal cmd.exe (NOT inside MSYS2 shell).
rem =====================================================================

cd /d "%~dp0"

echo.
echo === Quopus sidwrapper.dll Windows builder ===
echo.

rem ---------------------------------------------------------------------
rem Step 1: locate MSYS2
rem ---------------------------------------------------------------------
set "MSYS2_ROOT="
if defined MSYS2_DIR set "MSYS2_ROOT=%MSYS2_DIR%"

if not defined MSYS2_ROOT if exist "C:\msys64\usr\bin\bash.exe" (
    set "MSYS2_ROOT=C:\msys64"
)
if not defined MSYS2_ROOT if exist "C:\msys2\usr\bin\bash.exe" (
    set "MSYS2_ROOT=C:\msys2"
)
if not defined MSYS2_ROOT if exist "D:\msys64\usr\bin\bash.exe" (
    set "MSYS2_ROOT=D:\msys64"
)
if not defined MSYS2_ROOT if exist "%USERPROFILE%\msys64\usr\bin\bash.exe" (
    set "MSYS2_ROOT=%USERPROFILE%\msys64"
)

if not defined MSYS2_ROOT (
    echo [ERROR] MSYS2 not found. Install from https://www.msys2.org
    echo         and re-run this batch ^(default install C:\msys64 is
    echo         picked up automatically^).
    echo.
    echo If MSYS2 is installed in a non-standard location, set the
    echo MSYS2_DIR environment variable, e.g.:
    echo     set MSYS2_DIR=E:\tools\msys64
    echo     build_sidwrapper_windows.bat
    pause
    exit /b 1
)

echo [OK] Using MSYS2 at: %MSYS2_ROOT%

set "MINGW_BIN=%MSYS2_ROOT%\mingw64\bin"
set "BASH=%MSYS2_ROOT%\usr\bin\bash.exe"

if not exist "%BASH%" (
    echo [ERROR] bash.exe not found at %BASH%
    echo         Is MSYS2 properly installed?
    pause
    exit /b 1
)

rem ---------------------------------------------------------------------
rem Step 2: install toolchain + libsidplayfp via pacman
rem ---------------------------------------------------------------------
rem We pass --noconfirm so the install is non-interactive. First run
rem may take several minutes the first time it bootstraps pacman keys
rem and downloads the toolchain (~500 MB).
echo.
echo [1/4] Updating pacman and installing build dependencies...
echo       (first run may take a while, downloads ~500 MB)
echo.
"%BASH%" -lc "pacman -Sy --noconfirm && pacman -S --needed --noconfirm mingw-w64-x86_64-gcc mingw-w64-x86_64-libsidplayfp"
if errorlevel 1 (
    echo [ERROR] pacman failed. Try opening the MSYS2 MINGW64 shell
    echo         manually and running:
    echo             pacman -Syu
    echo             pacman -S mingw-w64-x86_64-toolchain mingw-w64-x86_64-libsidplayfp
    echo         then re-run this batch.
    pause
    exit /b 1
)

rem ---------------------------------------------------------------------
rem Step 3: compile sidwrapper.dll
rem ---------------------------------------------------------------------
echo.
echo [2/4] Compiling sidwrapper.dll...

if not exist "sidwrapper.cpp" (
    echo [ERROR] sidwrapper.cpp not found in current dir.
    echo         Place this batch next to sidwrapper.cpp and quopus.py.
    pause
    exit /b 1
)

rem We write the actual build commands into a small shell script and
rem execute that, instead of cramming everything into a single
rem `bash -lc "..."` invocation. cmd.exe's quote handling makes the
rem inline approach fragile - parens, pipes, and backslashes all
rem need triple-escaping and one mistake produces obscure errors
rem like "Das System kann den angegebenen Pfad nicht finden" before
rem bash even runs. A real script file dodges all of that.

rem Convert the current Windows path to a unix-style path that bash
rem can cd into directly. We do the conversion here in cmd via a
rem one-shot bash call, so the build script itself stays simple.
for /f "delims=" %%i in ('"%BASH%" -lc "cygpath -u '%CD%'"') do set "UNIX_CWD=%%i"

set "BUILD_SH=_build_sidwrapper.sh"
> "%BUILD_SH%" echo #!/bin/bash
>>"%BUILD_SH%" echo set -e
>>"%BUILD_SH%" echo cd "%UNIX_CWD%"
>>"%BUILD_SH%" echo CFLAGS=$(/mingw64/bin/pkg-config --cflags libsidplayfp 2^>/dev/null)
>>"%BUILD_SH%" echo LIBS=$(/mingw64/bin/pkg-config --libs libsidplayfp 2^>/dev/null)
>>"%BUILD_SH%" echo if [ -z "$LIBS" ]; then LIBS="-lsidplayfp"; fi
>>"%BUILD_SH%" echo echo "=== using CFLAGS: $CFLAGS"
>>"%BUILD_SH%" echo echo "=== using LIBS:   $LIBS"
>>"%BUILD_SH%" echo /mingw64/bin/g++ -O2 -shared sidwrapper.cpp $CFLAGS -o sidwrapper.dll $LIBS -lstdc++ -Wl,--out-implib,libsidwrapper.dll.a

rem Run the script through bash. Output goes to log file AND screen.
"%BASH%" -lc "bash %UNIX_CWD%/%BUILD_SH%" > build_sidwrapper.log 2>&1
type build_sidwrapper.log

if not exist "sidwrapper.dll" (
    echo.
    echo [ERROR] sidwrapper.dll was not produced.
    echo         See build_sidwrapper.log above for the compiler error.
    pause
    exit /b 1
)
del /q "%BUILD_SH%" 2>nul

rem ---------------------------------------------------------------------
rem Step 4: verify sid_set_roms is exported
rem ---------------------------------------------------------------------
echo.
echo [3/4] Verifying exports...
"%BASH%" -lc "/mingw64/bin/nm sidwrapper.dll | grep ' T sid_' | awk '{print \"  \" $3}'"
"%BASH%" -lc "/mingw64/bin/nm sidwrapper.dll | grep -q ' T sid_set_roms'"
if errorlevel 1 (
    echo [WARN] sid_set_roms NOT exported. Build is older than expected.
    echo        Check that sidwrapper.cpp is the version with the
    echo        sid_set_roms function.
    pause
    exit /b 1
)
echo [OK] sid_set_roms is exported.

rem ---------------------------------------------------------------------
rem Step 5: copy runtime DLLs next to sidwrapper.dll
rem ---------------------------------------------------------------------
echo.
echo [4/4] Copying runtime DLL dependencies...

rem Use ldd to discover what sidwrapper.dll actually links against,
rem then copy each /mingw64/bin/*.dll dependency into the current dir.
rem A few system DLLs (KERNEL32.dll etc.) come from C:\Windows and
rem must NOT be copied; we filter those out by checking the MSYS2 path.
"%BASH%" -lc "cd \"$(cygpath '%CD%')\" && /mingw64/bin/ldd sidwrapper.dll | awk '/=>/ {print $3}' | grep -i 'mingw64/bin' | while read p; do cp -v \"$p\" .; done"

echo.
echo === DONE ===
echo.
echo sidwrapper.dll and its runtime DLLs are now next to quopus.py.
echo Start Quopus and open a SID file - the header strip will show
echo "ROMs: missing" until you supply the C64 KERNAL/BASIC/CHARGEN.
echo.
echo Drop the three ROM files into the roms\ subfolder ^(see
echo roms\README.txt^) or install VICE - Quopus will auto-detect them.
echo.
pause
endlocal
