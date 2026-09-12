@echo off
rem ------------------------------------------------------------------
rem ASCII ONLY above :run. See the note after setlocal.
rem Keep these on separate lines, not in a ( ) block: %errorlevel% inside
rem a block is expanded when the block is parsed, i.e. before the child
rem has run, so the real exit code would always be lost.
rem ------------------------------------------------------------------
rem Detect '!' before delayed expansion is on. Afterwards %~f0 has
rem already lost it, so the check would never fire. %TEMP% is included:
rem the script runs from a copy made there, and a '!' in that path
rem breaks the copy just the same, but would otherwise be reported as
rem "could not copy the updater", which points at the wrong cause.
set "BANG="
echo."%~f0" "%~1" "%~2" "%TEMP%"| findstr /C:"!" >nul && set "BANG=1"

setlocal enabledelayedexpansion
if "%~3"=="--utf8" goto :run
rem Stop here, not after :run. With '!' in the path the re-entry itself
rem fails (!SELF! has already lost it), so nothing past this point runs.
rem The message stays ASCII: this is before the codepage is settled, and
rem printing Japanese here is the very fault this file was fixed for.
if defined BANG (
    echo ERROR: the path contains an exclamation mark.
    echo   NetBelt cannot update itself from a folder whose path
    echo   contains that character. This includes the TEMP folder
    echo   the updater runs from. Rename the folder, or extract
    echo   the new ZIP over this one by hand.
    pause
    exit /b 1
)
chcp 65001 >nul
set "SELF=%~f0"
set "A1=%~1"
set "A2=%~2"
rem Where to install. Taken here, because the run below happens from a
rem copy in TEMP, where %~dp0 would point at TEMP, not the install folder.
rem The trailing backslash is dropped so that a quoted "...\" does not
rem escape its own closing quote when passed on as an argument.
set "HOME_DIR=%~dp0"
if "!HOME_DIR:~-1!"=="\" set "HOME_DIR=!HOME_DIR:~0,-1!"
rem Run from a copy in TEMP. The update overwrites every file in the
rem install folder, this script included, and cmd.exe reads a batch file
rem by byte offset, so a script replaced while it runs carries on at a
rem meaningless position in the new file: fragments of lines get executed
rem and the whole sequence can run again. From TEMP the install folder is
rem only ever written to, never read from.
set "TMPRUNNER=%TEMP%\NetBeltUpdater_%RANDOM%.bat"
copy /y "!SELF!" "!TMPRUNNER!" >nul 2>&1
rem No falling back to running in place. The update overwrites every file
rem in the install folder, this script included, so running from there is
rem the very fault the copy exists to avoid. Stop instead.
if not exist "!TMPRUNNER!" goto :nocopy
rem Deliberately one line: nothing may be read from this file after the
rem child has replaced it.
cmd /d /c ""!TMPRUNNER!" "!A1!" "!A2!" --utf8 "!HOME_DIR!"" & set "RC=!errorlevel!" & del "!TMPRUNNER!" >nul 2>&1 & exit /b !RC!

:nocopy
echo ERROR: could not copy the updater to TEMP.
echo   The update has not been applied. Free some space in TEMP, or
echo   extract the new ZIP over this folder by hand.
pause
exit /b 1

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
REM インストール先。TEMP の写しから走るので %~dp0 は当てにならない。
REM 呼び出し元が第4引数で渡してくる（手で直接実行されたときだけ %~dp0）。
set "APP_DIR=%~dp0"
if not "%~4"=="" set "APP_DIR=%~4\"
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
REM 消すのは、このスクリプトが付けた名前（backup_netbelt_*）だけ。利用者が
REM 隣に置いた backup_* は機器コンフィグの退避先かもしれず、取り返しが
REM つかない。判定はディレクトリ自身の日付で行う。以前は forfiles /d を
REM 中身に対して使っていたため「7日以上前のファイルが1つでもある」で成立し、
REM 当日のファイルを含むディレクトリごと消していた。
echo [5/6] ファイルを更新中...
set "BK_PARENT=!APP_DIR:~0,-1!"
forfiles /p "!BK_PARENT!" /m "backup_netbelt_*" /d -7 /c "cmd /c if @isdir==TRUE rd /s /q @path" >nul 2>&1

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

REM 実行ファイルは直接上書きしない。コピーは宛先を先に切り詰めてから
REM 順に書くので、途中で止まる（コンソールを閉じる・電源断）と旧 exe は
REM 既に無く、末尾がゼロ埋めの exe だけが残る。一時名で置いてから改名で
REM 差し替える。同一ボリューム内の改名は途中で止まらない。
REM 一時名で置くのは exe だけ。同梱の他のファイルは従来どおり xcopy で
REM 直接上書きするので、下の差し替えが失敗すると旧 exe と新しい同梱
REM ファイルが混在する。全部を揃えてから入れ替えるには展開先ごと
REM 差し替える必要があるため、ここでは失敗時にその旨を伝えるに留める。
if exist "!SOURCE_DIR!\NetBelt.exe" ren "!SOURCE_DIR!\NetBelt.exe" "NetBelt.exe.new"

REM ファイルをコピー（上書き）
xcopy "!SOURCE_DIR!\*" "!APP_DIR!" /E /I /Y /Q >nul 2>&1
if errorlevel 1 (
    echo エラー: ファイルのコピーに失敗しました
    echo   アプリがまだ起動したままだと、上書きできません
    echo   NetBelt.exe は旧版のままですが、同梱の他のファイルは
    echo   一部またはすべてが新しい版に置き換わっている場合があります。
    del "!APP_DIR!NetBelt.exe.new" 2>nul
    rd /s /q "!TEMP_DIR!" 2>nul
    pause
    exit /b 1
)

REM 更新に実行ファイルが入っていたかを確かめる。xcopy の戻り値
REM だけでは分からない。見るのはコピー先ではなくコピー元。上の改名を
REM 実行できたときだけ NetBelt.exe.new ができるから。コピー先を
REM 見ると、前回の更新が改名の直前で止まって残した
REM NetBelt.exe.new が条件を満たし、exe を含まない zip でも
REM 動いている exe をその残骸で上書きしてしまう。
if not exist "!SOURCE_DIR!\NetBelt.exe.new" (
    echo エラー: 更新ファイルに NetBelt.exe が含まれていません
    echo   場所: !SOURCE_DIR!
    echo   NetBelt.exe は旧版のままですが、同梱の他のファイルは
    echo   既に新しい版へ置き換わっています。
    rd /s /q "!TEMP_DIR!" 2>nul
    pause
    exit /b 1
)
move /y "!APP_DIR!NetBelt.exe.new" "!APP_DIR!NetBelt.exe" >nul 2>&1
if errorlevel 1 (
    echo エラー: NetBelt.exe を差し替えられませんでした
    echo   アプリがまだ起動したままだと、差し替えられません
    echo   NetBelt.exe は旧版のままですが、同梱の他のファイルは
    echo   既に新しい版へ置き換わっています。アプリを終了してから
    echo   もう一度更新してください。
    del "!APP_DIR!NetBelt.exe.new" 2>nul
    rd /s /q "!TEMP_DIR!" 2>nul
    pause
    exit /b 1
)
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
