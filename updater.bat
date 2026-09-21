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

rem This run stops without writing anything into the install folder, so the
rem copy NetBelt made for it (the update folder's NetBelt-apply-*.zip and
rem its .sha256 / .version) is dead weight: the original ZIP is still there,
rem and the copy would otherwise be offered as a pending update of its own.
rem Do it here, while delayed expansion is still off - below this point a
rem '!' in the path is eaten before del could ever see it. The routine at
rem the end of the file does the same thing, but cannot be called from this
rem half: everything above :run must stay ASCII (see the note at the top).
rem The message and the exit stay further down, after the re-entry check.
if not defined BANG goto :bang_checked
if "%~3"=="--utf8" goto :bang_checked
set "ZIP_NAME=x"
for %%z in ("%~1") do set "ZIP_NAME=%%~nxz"
if /i not "%ZIP_NAME:~0,14%"=="NetBelt-apply-" goto :bang_checked
del "%~1" 2>nul
del "%~1.sha256" 2>nul
del "%~1.version" 2>nul
:bang_checked

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
rem One folder per run, claimed with md. cmd seeds %RANDOM% from the
rem clock, so two updaters started in the same moment draw the same
rem numbers: they would share both the copy below and the folder the
rem ZIP is unpacked into, and whichever finished first would delete
rem the other one's files out from under it. md fails when the name
rem is already taken, which is what makes the claim exclusive; a
rem leftover from an earlier run only costs one more try.
set "TRY=0"
:claim
set /a TRY+=1
set "WORK_DIR=%TEMP%\NetBeltUpdate_!TRY!_!RANDOM!"
md "!WORK_DIR!" 2>nul
if not errorlevel 1 goto :claimed
if !TRY! lss 20 goto :claim
goto :nowork

:claimed
set "TMPRUNNER=!WORK_DIR!\updater.bat"
copy /y "!SELF!" "!TMPRUNNER!" >nul 2>&1
rem No falling back to running in place. The update overwrites every file
rem in the install folder, this script included, so running from there is
rem the very fault the copy exists to avoid. Stop instead.
if not exist "!TMPRUNNER!" goto :nocopy
rem Deliberately one line. The child unpacks the new release over the
rem install folder, this file included, so by the time it returns this
rem script has been replaced on disk. cmd reads a batch line by line
rem from the file, so anything written below would be read out of the
rem new contents instead. Measured three times: with the launch, the
rem exit code, the cleanup and the exit joined by &, the tail runs and
rem the exit code survives; split across lines, nothing after the child
rem runs at all, which would leak the work folder in TEMP.
rem (An earlier comment here claimed the opposite. It was wrong: the
rem experiment behind it never replaced the parent file.)
cmd /d /c ""!TMPRUNNER!" "!A1!" "!A2!" --utf8 "!HOME_DIR!" "!WORK_DIR!"" & set "RC=!errorlevel!" & rd /s /q "!WORK_DIR!" >nul 2>&1 & exit /b !RC!

:nowork
rem Nothing in the install folder has been written, so drop the apply copy
rem (see the note beside the '!' check above for why it is spelled out here
rem instead of calling :drop_apply_copy).
set "ZIP_NAME=x"
for %%z in ("!A1!") do set "ZIP_NAME=%%~nxz"
if /i "!ZIP_NAME:~0,14!"=="NetBelt-apply-" (
    del "!A1!" 2>nul
    del "!A1!.sha256" 2>nul
    del "!A1!.version" 2>nul
)
echo ERROR: could not create a work folder in TEMP.
echo   Twenty names were tried, so TEMP is most likely full, read-only
echo   or missing. The update has not been applied. Fix TEMP, or
echo   extract the new ZIP over this folder by hand.
pause
exit /b 1

:nocopy
rd /s /q "!WORK_DIR!" 2>nul
rem Same as :nowork: nothing in the install folder has been written yet.
set "ZIP_NAME=x"
for %%z in ("!A1!") do set "ZIP_NAME=%%~nxz"
if /i "!ZIP_NAME:~0,14!"=="NetBelt-apply-" (
    del "!A1!" 2>nul
    del "!A1!.sha256" 2>nul
    del "!A1!.version" 2>nul
)
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
REM   %4 = インストール先（内部用。TEMP の写しでは %~dp0 が使えない）
REM   %5 = 親が確保した作業フォルダ（内部用。展開先の親になる）
REM ================================================================

echo ================================================
echo  NetBelt 自動更新
echo ================================================
echo.

REM 引数チェック
REM 第1引数が無いときは、消すべき写しの在り処そのものが分からないので、
REM ここだけは何も消さずに止まる（NetBelt は必ず ZIP のパスを渡す）。
if "%~1"=="" (
    echo エラー: ZIPファイルパスが指定されていません
    pause
    exit /b 1
)

REM ZIP_FILE は第2引数の検査より前に決める。この中止もインストール先へ
REM 何も書かずに終わるので、他の中止と同じく :drop_apply_copy へ写しの
REM 在り処を渡せるようにしておく。実測（検査役 cx5g-verify-release の
REM p3_nocopy_and_args.py）: 以前はここだけが呼ばずに止まり、更新フォルダに
REM NetBelt-apply-*.zip と控え 2 つが残っていた。
set "ZIP_FILE=%~1"

if "%~2"=="" (
    echo エラー: アプリケーションパスが指定されていません
    call :drop_apply_copy
    pause
    exit /b 1
)

set "APP_PATH=%~2"
REM インストール先。TEMP の写しから走るので %~dp0 は当てにならない。
REM 呼び出し元が第4引数で渡してくる（手で直接実行されたときだけ %~dp0）。
set "APP_DIR=%~dp0"
if not "%~4"=="" set "APP_DIR=%~4\"
REM 展開先。親が確保した専用フォルダの中に置く。%RANDOM% で名前を
REM 作ると、同じ瞬間に始まった別の更新と同じ名前になる（cmd は
REM %RANDOM% をプロセス開始時の時計で種付けするため）。
REM ここへ来るのは第3引数が --utf8 のときだけで、それを渡すのは
REM 上の再入だけ。つまり第5引数は必ず付く。下の既定値は、切り分けの
REM ために --utf8 を手で渡して直接叩いたときにしか使われない。
REM その道では名前の重複を防げないので、常用しないこと。
set "TEMP_DIR=%TEMP%\NetBeltUpdate_%RANDOM%"
if not "%~5"=="" set "TEMP_DIR=%~5\zip"

REM 新しい exe をいったん置く一時名に混ぜる、実行ごとの目印。
REM 親が md で排他確保した作業フォルダの名前をそのまま借りる。
REM TEMP 側と違い、インストール先は他の更新と共有し得るためで、
REM 詳しい理由は下の [5/6] の手前に書いた。第5引数が無いのは
REM --utf8 を手で渡して直接叩いたときだけなので、そこは TEMP_DIR と
REM 同じく %RANDOM% に落とす（重複を防げないので常用しないこと）。
set "STAMP_FROM=%~5"
set "STAMP=%RANDOM%"
if not "!STAMP_FROM!"=="" for %%w in ("!STAMP_FROM!") do set "STAMP=%%~nxw"
set "STAGED_NAME=NetBelt.exe.!STAMP!.new"
set "STAGED_PATH=!APP_DIR!!STAGED_NAME!"

echo [1/6] 更新情報
echo   ZIPファイル: !ZIP_FILE!
echo   アプリパス: !APP_PATH!
echo   インストール先: !APP_DIR!
echo.

REM ZIPファイルの存在確認
if not exist "!ZIP_FILE!" (
    echo エラー: ZIPファイルが見つかりません
    echo   パス: !ZIP_FILE!
    call :drop_apply_copy
    pause
    exit /b 1
)

REM 動いている実行ファイルの名前を確かめる。差し替えるのは
REM !APP_DIR!NetBelt.exe だけなので、exe を改名して使っていると
REM 動いている実体は旧版のまま残り、身に覚えのない NetBelt.exe が
REM 増えるだけになる。それでも成功として終わっていたため、次の起動でも
REM 同じ更新が見つかり、通知が繰り返されていた。展開も削除もまだ
REM していないこの位置で止める。
for %%f in ("!APP_PATH!") do set "EXE_NAME=%%~nxf"
if /i not "!EXE_NAME!"=="NetBelt.exe" (
    echo エラー: 実行ファイルの名前が NetBelt.exe ではありません
    echo   実行ファイル: !EXE_NAME!
    echo   自動更新が差し替えられるのは NetBelt.exe だけです。このまま
    echo   進めても !EXE_NAME! は旧版のまま残り、別名の NetBelt.exe が
    echo   増えるだけになるため、更新を当てずに中止しました。
    echo   名前を NetBelt.exe へ戻すか、新しい ZIP を手で展開してください。
    call :drop_apply_copy
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
    call :drop_apply_copy
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
    call :drop_apply_copy
    pause
    exit /b 1
)
echo   展開完了
echo.

REM アプリの隣にあるフォルダは、名前にかかわらず削除しない。
REM 以前はここで backup_netbelt_* を「スクリプト自身が付けた名前」として
REM 7日で掃除していたが、この名前のフォルダを作る実装は NetBelt のどこにも
REM 無い。実際にあるなら利用者が置いたものであり、機器コンフィグの退避先
REM かもしれない。更新のついでに無断で消してよいものは一つも無い。
echo [5/6] ファイルを更新中...

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
REM 一時名は実行ごとに変える（上で組み立てた STAGED_NAME）。TEMP の作業場所は md で
REM 排他確保しているが、インストール先は同じ場所を指す別の更新と
REM 共有し得る。固定名にすると、その 1 つのファイルを取り合うことに
REM なり、実測では先に改名した側が相手の exe を据えたうえで
REM 「更新が完了しました！」と表示して exit 0 を返し、もう一方は
REM 対象が消えているために「NetBelt.exe は旧版のままです」という
REM 事実と違う失敗を出していた。
REM 残る制限: 同梱の他のファイルは名前を変えられない（配布物の
REM 一部そのもの）ため、同じインストール先へ同時に更新をかけると、
REM どちらの版のファイルが残るかは混ざったままになる。
REM 更新に実行ファイルが入っているかは、インストール先へ何か書く前に
REM 確かめる。見るのはコピー先ではなくコピー元。コピー先を見ると、
REM 前回の更新が改名の直前で止まって残した一時名の exe が条件を
REM 満たしてしまう。以前はこの判定を xcopy の後に置いていたため、
REM exe の無い更新でも同梱の他のファイルだけが置き換わっていた。
if not exist "!SOURCE_DIR!\NetBelt.exe" (
    echo エラー: 更新ファイルに NetBelt.exe が含まれていません
    echo   場所: !SOURCE_DIR!
    echo   インストール先のファイルは何も変えていません。
    rd /s /q "!TEMP_DIR!" 2>nul
    call :drop_apply_copy
    pause
    exit /b 1
)
REM 改名の結果は必ず確かめる。展開した exe をウイルス対策ソフトや
REM インデクサが削除共有なしで開いていると、ren だけが失敗する。
REM 確かめずに進んだ以前の版は、xcopy が元の名前のまま exe を
REM インストール先へ直接上書きし、途中で止まると起動できない exe
REM だけが残った（実測）。そのうえ「含まれていません」と事実と逆の
REM 報告をしていた。走査は短いので少し待ってやり直し、それでも
REM 移せなければ、インストール先へ何も書かずに止める。
set "REN_TRY=0"
:stage_exe
set /a REN_TRY+=1
ren "!SOURCE_DIR!\NetBelt.exe" "!STAGED_NAME!" >nul 2>&1
if exist "!SOURCE_DIR!\!STAGED_NAME!" goto :staged
if !REN_TRY! lss 5 (
    ping -n 2 127.0.0.1 >nul 2>&1
    goto :stage_exe
)
echo エラー: 展開した NetBelt.exe を一時名へ移せませんでした
echo   ウイルス対策ソフトなど、別のプログラムが展開したファイルを
echo   開いている可能性があります。インストール先のファイルは
echo   何も変えていません。しばらく待ってから、もう一度更新してください。
rd /s /q "!TEMP_DIR!" 2>nul
call :drop_apply_copy
pause
exit /b 1
:staged

REM ここから先はインストール先を書き換える。その前に、同じインストール先を
REM 指す別の更新と重ならないよう、インストール先に目印フォルダを作って
REM 排他を取る。md は既にある名前で失敗するので、これが排他になる。
REM TEMP の作業場所は既に md で確保しているが、守られているのは TEMP 側
REM だけだった。実測（cx5b-release の r02_shared_appdir.py）: A を move の
REM 直後で止めて B を最後まで走らせると、A は A の版を承認したのに [6/6] で
REM 起動したのは B の exe で、両方が「更新が完了しました！」を出した。
REM 自然に重ねた試験では A の exe と B の同梱ファイルが混在し、B は
REM 「NetBelt.exe は旧版のまま」と事実と違う案内を出していた。
REM 目印は :release_lock で必ず外す（成功・失敗・中止のどれでも）。
REM 目印の中へ holder.txt を書くので、フォルダの更新日時＝確保した時刻に
REM なる。異常終了（コンソールを閉じられた等）で残った目印は、10分より
REM 古ければ取り除いて続ける。
REM その回収は「古いか見てから消す」ではなく「ren でつかんでから消す」。
REM 実測（検査役 cx5j-check-release の p2_stale_lock_race.py）: 古い目印を
REM 二つの更新がともに「古い」と判定すると、先に消したほうが取り直した
REM 新しい目印を、後から来たほうの削除が消してしまい、両方が排他を持って
REM 両方が「更新が完了しました！」を出した。ren は同時に 1 つしか成功
REM しないので、負けた側は何も消せずに中止できる。
REM つかんだ後にもう一度古さを見るのは、見てから掴むまでの間に別の更新が
REM 取り直しているかもしれないため。そのときは元の名前へ戻して譲る。
REM 目印を作れない理由には、重なり以外（インストール先へ書けない）もある。
REM どちらもここで中止するが、待てば直るものかどうかが違うので、下の
REM probe で見分けて案内を変える。
set "LOCK_DIR=!APP_DIR!NetBelt-update-lock"
set "LOCK_OLD_NAME=NetBelt-update-lock.!STAMP!.old"
set "LOCK_OLD=!APP_DIR!!LOCK_OLD_NAME!"
set "LOCK_HELD="
set "LOCK_STAMPED="
set "LOCK_TRY=0"
:claim_lock
set /a LOCK_TRY+=1
md "!LOCK_DIR!" 2>nul
if not errorlevel 1 goto :lock_claimed
REM md が失敗する理由は 2 つある。同じ名前が既にある（＝同じインストール先
REM への更新が重なった）か、そのフォルダへそもそも書けないか。両方を重なりと
REM して扱っていたため、書けない場所（Program Files 配下など）へ置いた利用者
REM には、待っても直らないものを待たせていた。実測（8d316a7、
REM tests/test_updater_appdir_unwritable.py と同じ小さな作り物で、インストール
REM 先の書き込みを icacls で拒否）: 「エラー: 別の更新が進行中です」で exit 1。
REM 誰とも重ならない名前（目印と同じ STAMP を借りる）で作ってみれば、どちらの
REM 理由かが分かる。作れたときは片付けて、これまでどおり重なりとして扱う。
set "PROBE_DIR=!APP_DIR!NetBelt-update-probe.!STAMP!"
md "!PROBE_DIR!" 2>nul
if not exist "!PROBE_DIR!" goto :lock_nowrite
rd /s /q "!PROBE_DIR!" 2>nul
REM 名前が取られている理由が「更新の目印」以外のこともある。実測（検査役
REM cx5j-check-release の p1_stale_lock_content.py）: 同じ名前のフォルダへ
REM 利用者が memo.txt と switch-config\core1.cfg を置いていると、更新日時が
REM 10分より古いというだけで目印とみなして丸ごと消し、そのまま完走していた
REM （伝えたのは「前の更新が残した目印を取り除きました」の 1 行だけ）。
REM 同じ名前のファイルでも、ren でつかんでから rd に失敗し、事実と違う
REM 「別の更新が進行中です」で止まっていた。
REM 更新が作る目印は「md の直後（空）」か「holder.txt 入り」のどちらかに
REM しかならない。それ以外＝ファイル、または holder.txt の無い中身つき
REM フォルダは、古さを見るより先に、消さずに中止する（利用者の決定
REM 2026-09-20 / release-01）。
set "PS_LOCK=!LOCK_DIR!"
call :lock_is_foreign
if not errorlevel 1 goto :lock_foreign
if !LOCK_TRY! geq 2 goto :lock_busy
call :lock_is_stale
if errorlevel 1 goto :lock_busy
ren "!LOCK_DIR!" "!LOCK_OLD_NAME!" 2>nul
if not exist "!LOCK_OLD!" goto :lock_busy
set "PS_LOCK=!LOCK_OLD!"
call :lock_is_stale
if errorlevel 1 goto :lock_put_back
rd /s /q "!LOCK_OLD!" 2>nul
if exist "!LOCK_OLD!" goto :lock_put_back
echo   前の更新が残した目印を取り除きました
goto :claim_lock

REM つかんだのは生きている目印だった（見てから掴むまでの間に別の更新が
REM 取り直した）か、片付けられなかった。元の名前へ戻して譲る。戻せない
REM のは、その間に別の更新が新しい目印を作ったときで、名前から外れた以上
REM もう誰の排他にもならないので、置き去りにせず消す。
:lock_put_back
ren "!LOCK_OLD!" "NetBelt-update-lock" 2>nul
if exist "!LOCK_OLD!" rd /s /q "!LOCK_OLD!" 2>nul
goto :lock_busy

:lock_busy
echo エラー: 別の更新が進行中です
echo   同じインストール先への更新が既に動いています。インストール先の
echo   ファイルは何も変えていません。先の更新が終わるのを待ってから、
echo   もう一度お試しください。
rd /s /q "!TEMP_DIR!" 2>nul
call :drop_apply_copy
pause
exit /b 1

:lock_nowrite
echo エラー: インストール先へ書き込めません
echo   場所: !APP_DIR!
echo   このフォルダへ書き込む権限が無いため、更新を当てられません。
echo   インストール先のファイルは何も変えていません。書き込める場所へ
echo   NetBelt を置き直すか、管理者に権限を確かめてもらってください。
rd /s /q "!TEMP_DIR!" 2>nul
call :drop_apply_copy
pause
exit /b 1

:lock_foreign
echo エラー: NetBelt-update-lock という名前のものがありますが、更新が作った目印ではないようです。
echo   中身を確かめて名前を変えるか移動してください。
echo   場所: !LOCK_DIR!
echo   インストール先のファイルは何も変えていません。
rd /s /q "!TEMP_DIR!" 2>nul
call :drop_apply_copy
pause
exit /b 1

:lock_claimed
set "LOCK_HELD=1"
REM 誰の目印かを中へ書く。:release_lock は中身が自分の識別子のときだけ
REM 外す。識別子は親が md で確保した作業フォルダの名前（STAMP）。
REM 括弧で囲むのは、STAMP が数字で終わると echo の直前の 1 桁が
REM リダイレクト先のハンドル番号として読まれてしまうため。
(echo !STAMP!)>"!LOCK_DIR!\holder.txt" 2>nul
if exist "!LOCK_DIR!\holder.txt" set "LOCK_STAMPED=1"

REM 前の実行が置き去りにした一時名の exe を片付ける。差し替えが 5 回とも
REM 失敗すると、後始末の del も同じ理由（削除共有なしで掴まれている）で
REM 失敗する。実測（cx5c-verify-release の a_leftover_staged.py）: exit 1 の
REM あともインストール先に NetBelt.exe.NetBeltUpdate_1_697.new が残り、
REM 掴みを手放しても誰も消さなかった。一時名は実行ごとに変わるので、
REM 失敗のたびに配布物 1 個ぶんが積まれていく。
REM 自分の一時名はまだ展開先（TEMP）にあり、目印を持っている間は他の更新が
REM インストール先へ置くこともないので、ここにあるのは前の実行のぶんだけ。
REM 掴まれたままのものは消せないが、黙って飛ばして続ける（次の更新が拾う）。
REM 拡張子を見直すのは、ワイルドカードが 8.3 形式の短い名前にも当たるため。
for %%s in ("!APP_DIR!NetBelt.exe.*.new") do (
    if /i "%%~xs"==".new" del "%%~fs" 2>nul
)

REM ファイルをコピー（上書き）
xcopy "!SOURCE_DIR!\*" "!APP_DIR!" /E /I /Y /Q >nul 2>&1
if errorlevel 1 (
    echo エラー: ファイルのコピーに失敗しました
    echo   アプリがまだ起動したままだと、上書きできません
    echo   NetBelt.exe は旧版のままですが、同梱の他のファイルは
    echo   一部またはすべてが新しい版に置き換わっている場合があります。
    del "!STAGED_PATH!" 2>nul
    rd /s /q "!TEMP_DIR!" 2>nul
    call :release_lock
    pause
    exit /b 1
)
REM 差し替えも ren と同じく待ってやり直す。xcopy が置いたばかりの
REM 一時名の exe も、ウイルス対策ソフトなどが削除共有なしで短く開く。
REM 1 回きりだった以前の版は、走査が 1.5 秒で終わる場合でも失敗し、
REM 同梱の他のファイルだけが新しい版になった状態で止まっていた（実測）。
set "MOVE_TRY=0"
:swap_exe
set /a MOVE_TRY+=1
move /y "!STAGED_PATH!" "!APP_DIR!NetBelt.exe" >nul 2>&1
if not errorlevel 1 goto :swapped
if !MOVE_TRY! lss 5 (
    ping -n 2 127.0.0.1 >nul 2>&1
    goto :swap_exe
)
echo エラー: NetBelt.exe を差し替えられませんでした
echo   アプリがまだ起動したままだと、差し替えられません
echo   NetBelt.exe は旧版のままですが、同梱の他のファイルは
echo   既に新しい版へ置き換わっています。アプリを終了してから
echo   もう一度更新してください。
del "!STAGED_PATH!" 2>nul
rd /s /q "!TEMP_DIR!" 2>nul
call :release_lock
pause
exit /b 1
:swapped
if not exist "!APP_DIR!NetBelt.exe" (
    echo エラー: 更新後の NetBelt.exe が見つかりません
    echo   場所: !APP_DIR!
    rd /s /q "!TEMP_DIR!" 2>nul
    call :release_lock
    pause
    exit /b 1
)
echo   更新完了
echo.

REM アプリケーションを再起動
REM start は成功しても errorlevel を 0 に戻さない。直前の失敗が残って
REM いると、起動できていても失敗と誤判定する。これが v1.1.0 の不具合で、
REM 長らく start の戻り値を見ない形にしていた。その代わり、起動できない
REM exe に差し替わっても「起動しました」と表示していた。
REM 直前に空の cmd を 0 で終わらせて errorlevel を均せば両立する。実測:
REM   均してから 起動できない exe を start -> 216
REM   均さず直前を 9 にして 起動できる exe -> 9   （これが v1.1.0 の形）
REM   均してから 起動できる   exe を start -> 0
echo [6/6] アプリケーションを再起動中...
if not exist "!APP_PATH!" (
    echo エラー: 実行ファイルが見つかりません
    echo   パス: !APP_PATH!
    rd /s /q "!TEMP_DIR!" 2>nul
    call :release_lock
    pause
    exit /b 1
)
cmd /d /c exit 0
start "" "!APP_PATH!"
if errorlevel 1 set "LAUNCH_FAILED=1"
if defined LAUNCH_FAILED (
    echo   起動できませんでした
    echo   更新そのものは当たっています。NetBelt.exe を手で起動してください。
) else (
    echo   起動しました
)
echo.

REM クリーンアップ
REM 起動できなかったときは ZIP と検証用のサイドカーを残す。消してしまうと、
REM 当て直す材料も、何を当てたのかを確かめる材料も無くなる。
echo クリーンアップ中...
ping -n 2 127.0.0.1 >nul 2>&1
rd /s /q "!TEMP_DIR!" 2>nul
REM 目印はここで外す。下の LAUNCH_FAILED の道は pause で止まるので、
REM その手前で外しておかないと、次の更新が読まれるまで待たされる。
call :release_lock
if not defined LAUNCH_FAILED del "!ZIP_FILE!" 2>nul
if not defined LAUNCH_FAILED del "!ZIP_FILE!.sha256" 2>nul
if not defined LAUNCH_FAILED del "!ZIP_FILE!.version" 2>nul
echo   完了
echo.

echo ================================================
echo  更新が完了しました！
echo ================================================
REM 残る制限: 起動できなかった場合も、更新そのもの（ファイルの差し替え）は
REM 当たっているため、上の表示と終了コード 0 は変えていない。呼び出し元は
REM 更新のために既に終了しているので、この値を読む相手がいない。伝わるのは
REM この画面だけなので、その場合は自動で閉じずに読ませる。
if defined LAUNCH_FAILED (
    echo.
    echo  ただし NetBelt.exe を起動できませんでした。
    echo  手で起動できないときは、残してある ZIP を展開し直してください。
    pause
    exit /b 0
)
ping -n 4 127.0.0.1 >nul 2>&1

exit /b 0

REM ================================================================
REM 適用用の写しを片付ける（call で呼ぶ）
REM ================================================================
REM NetBelt は適用の直前に、検証した ZIP の写し（更新フォルダの
REM NetBelt-apply-*.zip）と控え（.sha256 / .version）を作り、写しを
REM 渡してくる。元の ZIP は更新フォルダに残っているので、写しは当て直しの
REM 材料にもならない。以前は更新を当て終えたときしか消しておらず、
REM インストール先へ何も書く前に中止するたびに、配布 ZIP 1 個分の写しが
REM 検証記録つきの .zip として溜まり、未適用の更新の候補にも並んでいた。
REM インストール先へ何か書く前の中止だけがここを呼ぶ。消すのは、渡された
REM ZIP の名前が NetBelt-apply- で始まるときだけ（手で渡した ZIP は消さない）。
:drop_apply_copy
for %%z in ("!ZIP_FILE!") do set "ZIP_NAME=%%~nxz"
if /i not "!ZIP_NAME:~0,14!"=="NetBelt-apply-" exit /b 0
del "!ZIP_FILE!" 2>nul
del "!ZIP_FILE!.sha256" 2>nul
del "!ZIP_FILE!.version" 2>nul
exit /b 0

REM ================================================================
REM インストール先の目印を外す（call で呼ぶ）
REM ================================================================
REM 自分が確保したときだけ外す。目印を取れずに中止した側がここを通っても、
REM 動いているほうの目印を消してしまわないようにするため。
REM 確保したはずの目印が、走っている間に別の更新のものへ入れ替わることも
REM ある（上の :claim_lock の注を参照）。LOCK_HELD だけを見て消していた
REM ときは、そこで他人の排他まで外していた。中身の識別子で確かめる。
REM 識別子を書けなかったときだけ、以前と同じ無条件の削除にする。自分の
REM 目印を外せないほうが、次の更新を 10 分待たせる分だけ悪いため。
:release_lock
if not defined LOCK_HELD exit /b 0
set "LOCK_HELD="
if not defined LOCK_STAMPED goto :release_lock_rd
set "LOCK_OWNER="
set /p LOCK_OWNER=<"!LOCK_DIR!\holder.txt" 2>nul
if not "!LOCK_OWNER!"=="!STAMP!" exit /b 0
:release_lock_rd
rd /s /q "!LOCK_DIR!" 2>nul
exit /b 0

REM ================================================================
REM 目印が古い（＝異常終了の置き土産）かを見る（call で呼ぶ）
REM ================================================================
REM PS_LOCK に見るフォルダを入れて呼ぶ。古ければ errorlevel 0、そうで
REM なければ 1。フォルダの更新日時は holder.txt を書いた時刻＝確保した
REM 時刻になる。回収では 2 回呼ぶ（つかむ前と、つかんだ後）。
:lock_is_stale
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $d = Get-Item -LiteralPath $env:PS_LOCK -Force -ErrorAction Stop; if ($d.LastWriteTime -lt (Get-Date).AddMinutes(-10)) { exit 0 } } catch { }; exit 1"
exit /b !errorlevel!

REM ================================================================
REM その名前が、更新の目印でないものに使われていないかを見る（call で呼ぶ）
REM ================================================================
REM PS_LOCK に見るものを入れて呼ぶ。更新が作った目印に見えなければ
REM errorlevel 0、見えれば 1。目印は md の直後なら空、holder.txt を
REM 書いた後ならそれが入っている。ファイル、または holder.txt の無い
REM 中身つきフォルダは、利用者が置いたものとして扱う。
:lock_is_foreign
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { $i = Get-Item -LiteralPath $env:PS_LOCK -Force -ErrorAction Stop; if (-not $i.PSIsContainer) { exit 0 }; if (Test-Path -LiteralPath (Join-Path $i.FullName 'holder.txt')) { exit 1 }; if (@(Get-ChildItem -LiteralPath $i.FullName -Force -ErrorAction SilentlyContinue).Count -gt 0) { exit 0 } } catch { }; exit 1"
exit /b !errorlevel!
