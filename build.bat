@echo off
echo ================================
echo NetBelt ビルドスクリプト
echo ================================
echo.

echo [1/3] 既存のビルドをクリーンアップ中...
if exist "dist" (
    rmdir /s /q dist
    echo - dist フォルダを削除しました
)
if exist "build" (
    rmdir /s /q build
    echo - build フォルダを削除しました
)
echo.

echo [2/3] PyInstallerでビルド中...
echo このプロセスには数分かかる場合があります...
echo.
python -m PyInstaller --clean NetBelt.spec
echo.

if exist "dist\NetBelt.exe" (
    echo [3/3] ビルド成功！
    echo.
    echo 実行ファイルの場所: dist\NetBelt.exe
    echo.
    echo ================================
    echo ビルド完了！
    echo ================================
    echo.
    echo 配布方法:
    echo 1. dist フォルダ内の NetBelt.exe を他のユーザーに配布してください
    echo 2. このファイルは単独で動作します（Pythonインストール不要）
    echo 3. 初回起動時に config.json が自動生成されます
    echo.
) else (
    echo [3/3] ビルド失敗
    echo エラーが発生しました。上記のメッセージを確認してください。
    echo.
)

pause
