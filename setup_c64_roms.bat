@echo off
setlocal enabledelayedexpansion
rem =====================================================================
rem setup_c64_roms.bat
rem =====================================================================
rem Copies the C64 KERNAL/BASIC/CHARGEN ROMs from a VICE installation
rem into the Quopus roms\ folder so the SID player can play RSIDs.
rem
rem Looks in the standard WinVICE install paths. If WinVICE isn't
rem installed, ends with download instructions.
rem =====================================================================
cd /d "%~dp0"
if not exist roms mkdir roms

echo === Quopus C64 ROM finder ===
echo.

set "VICE_ROOT="
for %%P in (
    "C:\Program Files\WinVICE\C64"
    "C:\Program Files (x86)\WinVICE\C64"
    "C:\vice\C64"
    "C:\Program Files\VICE\C64"
    "C:\Program Files (x86)\VICE\C64"
    "%USERPROFILE%\vice\C64"
) do (
    if exist %%P (
        set "VICE_ROOT=%%~P"
        goto :found
    )
)

echo No VICE installation found in the usual places.
echo.
echo Either install VICE from https://vice-emu.sourceforge.io/
echo ^(the Windows builds still bundle the ROMs^), or download an
echo older VICE 3.x .tar.gz / .zip from
echo   https://sourceforge.net/projects/vice-emu/files/releases/
echo and copy these three files manually into the roms\ folder:
echo.
echo   kernal.901227-03.bin    8192 bytes
echo   basic.901226-01.bin     8192 bytes
echo   chargen.901225-01.bin   4096 bytes
echo.
echo After that, restart Quopus and re-open a SID. The header should
echo show "ROMs: OK".
pause
exit /b 1

:found
echo Found VICE C64 dir: %VICE_ROOT%
echo.

set "INSTALLED=0"
call :try_copy "kernal" 8192 "kernal.901227-03.bin"
call :try_copy "basic"  8192 "basic.901226-01.bin"
call :try_copy "chargen" 4096 "chargen.901225-01.bin"

echo.
if %INSTALLED% EQU 3 (
    echo === All three ROMs installed in roms\. SID playback ready. ===
) else if %INSTALLED% GTR 0 (
    echo === Installed %INSTALLED% of 3 ROMs. Some RSIDs may still fail. ===
) else (
    echo === No ROMs found in the VICE folder. ===
    echo Your VICE install may be a stripped distribution. Try an
    echo older VICE 3.x release - those bundle the ROMs.
)
pause
exit /b 0

rem ---------------------------------------------------------------------
rem :try_copy <name-substring> <expected-size> <target-name>
rem Find any file in %VICE_ROOT% whose name contains the substring,
rem verify size, copy to roms\<target-name>.
rem ---------------------------------------------------------------------
:try_copy
set "ROLE=%~1"
set "WANT_SIZE=%~2"
set "TARGET=%~3"
for %%F in ("%VICE_ROOT%\*%ROLE%*") do (
    set "SZ=%%~zF"
    if !SZ! EQU %WANT_SIZE% (
        copy /y "%%F" "roms\%TARGET%" >nul
        echo Copied %ROLE%: %%F  -^>  roms\%TARGET%
        set /a INSTALLED+=1
        goto :try_copy_done
    )
)
echo %ROLE%: nothing matching size %WANT_SIZE% found in VICE folder.
:try_copy_done
exit /b 0
