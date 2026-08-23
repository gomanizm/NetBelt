@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

REM ================================================================
REM NetBelt 自動更新スクリプト
REM ================================================================
REM 引数:
REM   %1 = ダウンロードしたZIPファイルのパス
REM   %2 = アプリケーション実行ファイルのパス
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

set ZIP_FILE=%~1
set APP_PATH=%~2
set APP_DIR=%~dp0
set TEMP_DIR=%TEMP%\NetBeltUpdate_%RANDOM%

echo [1/6] 更新情報
echo   ZIPファイル: %ZIP_FILE%
echo   アプリパス: %APP_PATH%
echo   インストール先: %APP_DIR%
echo.

REM ZIPファイルの存在確認
if not exist "%ZIP_FILE%" (
    echo エラー: ZIPファイルが見つかりません
    echo   パス: %ZIP_FILE%
    pause
    exit /b 1
)

REM アプリケーションの終了を待機
echo [2/6] アプリケーションの終了を待機中...
timeout /t 3 /nobreak >nul
echo   完了
echo.

REM 一時ディレクトリ作成
echo [3/6] 一時ディレクトリを作成中...
mkdir "%TEMP_DIR%" 2>nul
if not exist "%TEMP_DIR%" (
    echo エラー: 一時ディレクトリの作成に失敗しました
    pause
    exit /b 1
)
echo   作成完了: %TEMP_DIR%
echo.

REM ZIPファイルを展開
echo [4/6] ZIPファイルを展開中...
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Expand-Archive -Path '%ZIP_FILE%' -DestinationPath '%TEMP_DIR%' -Force; exit 0 } catch { Write-Host 'エラー:' $_.Exception.Message; exit 1 }"
if errorlevel 1 (
    echo エラー: ZIPファイルの展開に失敗しました
    rd /s /q "%TEMP_DIR%" 2>nul
    pause
    exit /b 1
)
echo   展開完了
echo.

REM バックアップディレクトリ作成（オプション）
echo [5/6] ファイルを更新中...
REM 古いバックアップを削除（7日以上前のもの）
for /d %%d in ("%APP_DIR%backup_*") do (
    set "BACKUP_DIR=%%d"
    set "DIR_NAME=%%~nd"
    REM 7日以上前のバックアップを削除
    forfiles /p "%%d" /d -7 >nul 2>&1
    if not errorlevel 1 (
        rd /s /q "%%d" 2>nul
    )
)

REM 新しいファイルでアップデート
REM 展開されたファイルを確認（ルートに直接あるか、サブフォルダか）
if exist "%TEMP_DIR%\NetBelt.exe" (
    set "SOURCE_DIR=%TEMP_DIR%"
) else (
    REM サブフォルダを探す
    for /d %%d in ("%TEMP_DIR%\*") do (
        if exist "%%d\NetBelt.exe" (
            set "SOURCE_DIR=%%d"
            goto :found_source
        )
    )
    set "SOURCE_DIR=%TEMP_DIR%"
)
:found_source

echo   コピー元: !SOURCE_DIR!
echo   コピー先: %APP_DIR%

REM ファイルをコピー（上書き）
xcopy "!SOURCE_DIR!\*" "%APP_DIR%" /E /I /Y /Q >nul 2>&1
if errorlevel 1 (
    echo 警告: 一部のファイルのコピーに失敗しました
) else (
    echo   更新完了
)
echo.

REM アプリケーションを再起動
echo [6/6] アプリケーションを再起動中...
start "" "%APP_PATH%"
if errorlevel 1 (
    echo エラー: アプリケーションの起動に失敗しました
    echo   パス: %APP_PATH%
    pause
    rd /s /q "%TEMP_DIR%" 2>nul
    exit /b 1
)
echo   起動完了
echo.

REM クリーンアップ
echo クリーンアップ中...
timeout /t 1 /nobreak >nul
rd /s /q "%TEMP_DIR%" 2>nul
del "%ZIP_FILE%" 2>nul
echo   完了
echo.

echo ================================================
echo  更新が完了しました！
echo ================================================
timeout /t 3 /nobreak >nul

exit /b 0