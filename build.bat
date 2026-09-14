@echo off
rem dist/ build/ の削除と spec の参照は相対パス。別のディレクトリから呼ぶと
rem そちらの dist/ build/ を消すので、先に自分の置き場所へ移る。
rem 移れなかったときは、必ずここで止める。cmd.exe は UNC パスを
rem カレントにできないので、共有フォルダから叩くとこの cd は失敗し、
rem カレントは呼び出し元のまま。そのまま進むと下の rmdir が消すのは
rem 呼び出し元の dist/ build/ になる。
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
rem 掃除しそこねると前回の exe がそのまま残る。今回のビルドが失敗しても
rem その exe を今回の成果物として「ビルド成功」と表示してしまうので、
rem ここで止める。exit /b は入れ子の括弧の中だと終了コードが伝わらない
rem ため、確認は括弧の外で行う。
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
rem exe の有無だけでは足りない。PyInstaller が落ちても、掃除しそこねた
rem 前回の exe が残っていれば「成功」に見えてしまう。
if errorlevel 1 (
    echo.
    echo [3/3] ビルド失敗
    echo PyInstaller がエラーで終了しました。上記のメッセージを確認してください。
    pause
    exit /b 1
)
echo.

if exist "dist\NetBelt.exe" (
    rem exe は自動更新の最後に updater.bat を自分の隣から探す。
    rem 入れ忘れると、更新を落とせても適用の直前で必ず失敗する。
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
) else (
    echo [3/3] ビルド失敗
    echo エラーが発生しました。上記のメッセージを確認してください。
    echo.
    pause
    exit /b 1
)

pause
