@echo off
rem Settle the codepage, then run this file again from the top.
rem The file is UTF-8 with no BOM, and cmd reads a batch file in the
rem console codepage (CP932 on a Japanese system) while it tracks
rem where to read next by byte offset. Japanese decoded as CP932 has
rem a different length, so the reader loses its place and the tail of
rem a line gets run as a command -- which sets errorlevel and can
rem flip the result of a later 'if errorlevel 1'.
rem chcp on its own does not cure it: cmd has already read ahead
rem under the old codepage. Measured from CP932, six runs each of a
rem good build and of a failing updater copy: as it was, with a bare
rem chcp, and with chcp plus goto, every run printed two 'is not
rem recognized' lines; re-running the file in a child cmd printed
rem none. updater.bat re-enters itself for the same reason.
rem Everything down to the re-entry must stay ASCII. Comments are
rem ASCII throughout anyway; Japanese lives in the echo lines.
if "%~1"=="--utf8" goto :start
chcp 65001 >nul
"%COMSPEC%" /d /c ""%~f0" --utf8"
exit /b %errorlevel%

:start
rem
rem dist/ and build/ are removed by relative path and the spec is read
rem by relative path, so move to this script's own folder first; called
rem from elsewhere this would delete that folder's dist/ and build/.
rem Stop when the move fails: cmd.exe cannot make a UNC path current,
rem so running this from a share leaves the caller's folder current.
cd /d "%~dp0"
if errorlevel 1 (
    echo ERROR: このスクリプトの置き場所へ移動できません。
    echo   場所: %~dp0
    echo   共有フォルダ ^(\\server\share^) から直接は実行できません。
    echo   ローカルのドライブへコピーしてから実行してください。
    pause
    exit /b 1
)
echo ================================
echo NetBelt ビルドスクリプト
echo ================================
echo.

echo [1/3] 既存のビルドをクリーンアップ中...
set "NETBELT_DIST_CLEANED="
if exist "dist" (
    rmdir /s /q dist
    set "NETBELT_DIST_CLEANED=1"
)
rem A failed clean leaves the previous exe in place, and a later
rem failure would then report that stale exe as this build's output.
rem Stop here. The check sits outside the parentheses because exit /b
rem inside a nested block does not carry the exit code out.
if exist "dist" (
    echo ERROR: dist フォルダを削除できませんでした。
    echo   NetBelt.exe を実行中なら、終了してからやり直してください。
    pause
    exit /b 1
)
if defined NETBELT_DIST_CLEANED echo - dist フォルダを削除しました
set "NETBELT_DIST_CLEANED="
if exist "build" (
    rmdir /s /q build
    echo - build フォルダを削除しました
)
echo.

echo [2/3] PyInstallerでビルド中...
echo このプロセスには数分かかる場合があります...
echo.
python -m PyInstaller --clean NetBelt.spec
rem The presence of the exe is not enough: if PyInstaller fails and the
rem clean also failed, the stale exe would still look like success.
if errorlevel 1 (
    echo.
    echo [3/3] ビルド失敗
    echo PyInstaller がエラーで終了しました。上記のメッセージを確認してください。
    pause
    exit /b 1
)
echo.

rem Bail out first, so nothing below sits inside parentheses: exit /b in
rem a nested block does not carry the exit code out, and the copy check
rem used to live two levels deep -- a failed copy still exited with 0.
if not exist "dist\NetBelt.exe" (
    echo [3/3] ビルド失敗
    echo エラーが発生しました。上記のメッセージを確認してください。
    echo.
    pause
    exit /b 1
)
rem The app looks for updater.bat next to the exe at the end of an
rem update. Without it every downloaded update fails at the last step.
copy /y updater.bat dist\ >nul
if errorlevel 1 (
    echo [3/3] ビルド失敗
    echo updater.bat を dist へコピーできませんでした。
    echo 自動更新が動かないため、中止します。
    pause
    exit /b 1
)
echo [3/3] ビルド成功！
echo.
echo 実行ファイルの場所: dist\NetBelt.exe
echo 更新用スクリプト: dist\updater.bat
echo.
echo ================================
echo ビルド完了！
echo ================================
echo.
echo 配布方法:
echo 1. dist フォルダ内の NetBelt.exe と updater.bat を、同じ
echo    フォルダに置いたまま配布してください
echo 2. updater.bat が exe の隣に無いと、自動更新が適用されません
echo 3. Python のインストールは不要です
echo 4. 初回起動時に config.json が自動生成されます
echo.

pause
