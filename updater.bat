@echo off
rem ------------------------------------------------------------------
rem ASCII ONLY above :run. See the note after setlocal.
rem Keep these on separate lines, not in a ( ) block: %errorlevel% inside
rem a block is expanded when the block is parsed, i.e. before the child
rem has run, so the real exit code would always be lost.
rem ------------------------------------------------------------------
rem Detect '!' before delayed expansion is on. Afterwards %~f0 has
rem already lost it, so the check would never fire.
set "BANG="
echo."%~f0" "%~1" "%~2"| findstr /C:"!" >nul && set "BANG=1"

setlocal enabledelayedexpansion
if "%~3"=="--utf8" goto :run
rem Stop here, not after :run. With '!' in the path the re-entry itself
rem fails (!SELF! has already lost it), so nothing past this point runs.
rem The message stays ASCII: this is before the codepage is settled, and
rem printing Japanese here is the very fault this file was fixed for.
if defined BANG (
    echo ERROR: the path contains an exclamation mark.
    echo   NetBelt cannot update itself from a folder whose path
    echo   contains that character. Rename the folder, or extract
    echo   the new ZIP over this one by hand.
    pause
    exit /b 1
)
chcp 65001 >nul
set "SELF=%~f0"
set "A1=%~1"
set "A2=%~2"
cmd /d /c ""!SELF!" "!A1!" "!A2!" --utf8"
exit /b !errorlevel!

:run
setlocal enabledelayedexpansion

REM ================================================================
REM NetBelt 自動更新スクリプト
REM ================================================================
REM 冒頭でコードページを決めてから、同じファイルへ入り直している。
REM cmd.exe はバッチファイルをバイト位置で読み直すため、ファイルの
REM 途中で chcp すると位置がずれ、日本語の行が二重に読まれたり途中で
REM 切れて、余りがコマンドとして実行される。実際 v1.1.0 の更新で
REM 「'ションを再起動中...' is not recognized」が出た。
REM そのため、入り直すまでの行は ASCII だけで書くこと。
REM ================================================================
REM 引数:
REM   %1 = ダウンロードしたZIPファイルのパス
REM   %2 = アプリケーション実行ファイルのパス
REM   %3 = --utf8（コードページ設定後の再入を示す内部用）
REM ================================================================

echo ================================================
echo  NetBelt 自動更新
echo ================================================
echo.

REM 引数チェック
if "%~1"=="" (
    echo エラー: ZIPファイルパスが指定されていません
    pause
    exit /b 1
)

if "%~2"=="" (
    echo エラー: アプリケーションパスが指定されていません
    pause
    exit /b 1
)

set "ZIP_FILE=%~1"
set "APP_PATH=%~2"
set "APP_DIR=%~dp0"
set "TEMP_DIR=%TEMP%\NetBeltUpdate_%RANDOM%"

echo [1/6] 更新情報
echo   ZIPファイル: !ZIP_FILE!
echo   アプリパス: !APP_PATH!
echo   インストール先: !APP_DIR!
echo.

REM ZIPファイルの存在確認
if not exist "!ZIP_FILE!" (
    echo エラー: ZIPファイルが見つかりません
    echo   パス: !ZIP_FILE!
    pause
    exit /b 1
)

REM アプリケーションの終了を待機
REM timeout は標準入力がリダイレクトされていると失敗し、errorlevel を
REM 残す。待つだけなので ping を使う（どの Windows にもある）。
echo [2/6] アプリケーションの終了を待機中...
ping -n 4 127.0.0.1 >nul 2>&1
echo   完了
echo.

REM 一時ディレクトリ作成
echo [3/6] 一時ディレクトリを作成中...
mkdir "!TEMP_DIR!" 2>nul
if not exist "!TEMP_DIR!" (
    echo エラー: 一時ディレクトリの作成に失敗しました
    pause
    exit /b 1
)
echo   作成完了: !TEMP_DIR!
echo.

REM ZIPファイルを展開
echo [4/6] ZIPファイルを展開中...
REM PowerShell の '...' に生のパスを埋めると、パスに ' が入っただけで
REM 文字列が閉じて壊れる。環境変数で渡せば引用符の問題が起きない。
set "PS_ZIP=!ZIP_FILE!"
set "PS_DEST=!TEMP_DIR!"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Expand-Archive -LiteralPath $env:PS_ZIP -DestinationPath $env:PS_DEST -Force; exit 0 } catch { Write-Host 'エラー:' $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo エラー: ZIPファイルの展開に失敗しました
    rd /s /q "!TEMP_DIR!" 2>nul
    pause
    exit /b 1
)
echo   展開完了
echo.

REM 古いバックアップを削除（7日以上前のもの）
echo [5/6] ファイルを更新中...
for /d %%d in ("!APP_DIR!backup_*") do (
    forfiles /p "%%d" /d -7 >nul 2>&1
    if not errorlevel 1 (
        rd /s /q "%%d" 2>nul
    )
)

REM 展開されたファイルを確認（ルートに直接あるか、サブフォルダか）
if exist "!TEMP_DIR!\NetBelt.exe" (
    set "SOURCE_DIR=!TEMP_DIR!"
) else (
    REM サブフォルダを探す
    for /d %%d in ("!TEMP_DIR!\*") do (
        if exist "%%d\NetBelt.exe" (
            set "SOURCE_DIR=%%d"
            goto :found_source
        )
    )
    set "SOURCE_DIR=!TEMP_DIR!"
)
:found_source

echo   コピー元: !SOURCE_DIR!
echo   コピー先: !APP_DIR!

REM ファイルをコピー（上書き）
xcopy "!SOURCE_DIR!\*" "!APP_DIR!" /E /I /Y /Q >nul 2>&1
if errorlevel 1 (
    echo エラー: ファイルのコピーに失敗しました
    echo   アプリがまだ起動したままだと、上書きできません
    rd /s /q "!TEMP_DIR!" 2>nul
    pause
    exit /b 1
)

REM コピーできたことを確認する。xcopy の戻り値だけでは、
REM 肝心の実行ファイルが置かれたかどうかは分からない。
if not exist "!APP_DIR!NetBelt.exe" (
    echo エラー: 更新後の NetBelt.exe が見つかりません
    echo   場所: !APP_DIR!
    rd /s /q "!TEMP_DIR!" 2>nul
    pause
    exit /b 1
)
echo   更新完了
echo.

REM アプリケーションを再起動
REM start は成功しても errorlevel を 0 に戻さない。直前の失敗が残って
REM いると、起動できていても失敗と誤判定する。start の戻り値では判定せず、
REM 起動する前に実行ファイルの存在を確かめる。
echo [6/6] アプリケーションを再起動中...
if not exist "!APP_PATH!" (
    echo エラー: 実行ファイルが見つかりません
    echo   パス: !APP_PATH!
    rd /s /q "!TEMP_DIR!" 2>nul
    pause
    exit /b 1
)
start "" "!APP_PATH!"
echo   起動しました
echo.

REM クリーンアップ
echo クリーンアップ中...
ping -n 2 127.0.0.1 >nul 2>&1
rd /s /q "!TEMP_DIR!" 2>nul
del "!ZIP_FILE!" 2>nul
del "!ZIP_FILE!.sha256" 2>nul
del "!ZIP_FILE!.version" 2>nul
echo   完了
echo.

echo ================================================
echo  更新が完了しました！
echo ================================================
ping -n 4 127.0.0.1 >nul 2>&1

exit /b 0
