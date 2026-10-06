# NetBelt

Windows 専用の PyQt6 アプリ。テストの合否は Windows の CI
（`.github/workflows/tests.yml`。PR と main への push で走る）で決める。

## 個人の情報を書かない

コミットの author / committer は noreply のアドレスのままにする。
本名、個人のメールアドレス、PC のユーザー名やホームフォルダのパスを、
コミット・ファイル・PR・コメントに書かない。

## クラウドセッション（Linux）でのテスト

`.claude/hooks/session-start.sh` が起動のたびに準備する。venv は
`~/.venvs/netbelt` で、`PATH` と `QT_QPA_PLATFORM=offscreen` も設定される。
Consolas と Courier New の代わりのフォントも入れる（無いと、ターミナルの描画・
選択と設定ダイアログのテストが 5 件落ちる）。

- 変えたところのテストを、ファイルか ID で指定して走らせる。
  `python -m pytest -q tests/test_xxx.py`
  パスを付けない実行（`-k` で絞っても）は、下の 4 つの収集エラーで中断し、
  1 件も走らない。
- 全件を 1 プロセスで流さない。次の 3 つは、更新の適用が Linux で失敗して
  （`subprocess.CREATE_NEW_CONSOLE` が無い）出るモーダルのエラーダイアログを
  誰も閉じず、止まったままになる。
  `test_pending_update_quit.py`・`test_update_restart_env.py`・`test_updater_script.py`
- 次の 4 つは、モジュールの読み込みで `ctypes.windll` に触れ、収集で落ちる。
  1 つでも含めると、pytest は全体を中断する。
  `test_updater_apply_copy_cleanup.py`・`test_updater_exe_rename_blocked.py`・
  `test_updater_exe_swap_retry.py`・`test_updater_staged_exe_sweep.py`
- `test_server_status_ui.py` は、テストがすべて通ったあと、終わりの後片付け
  （`tests/conftest.py` の `qapp`）で segfault する。
  `test_ssh_resize_after_transport_recovers.py` は、前提の「TCP が詰まった」
  状態が Linux ではタイミング次第で作れず、たいてい落ちる（ときどき通る）。
- ほかにも約 120 件が Linux でだけ落ちる。Windows にしか無いものに頼って
  いるか、コンテナの事情によるもので、直す対象ではない。主なもの:
  DPAPI（パスワードの暗号化）、`ctypes.windll`、`subprocess.CREATE_NEW_CONSOLE`、
  大文字と小文字を区別しないパスと 8.3 の短い名前、宛先があると断る Windows の
  `os.rename`（Linux では上書きする）、Windows のファイルの共有ロック、cmd.exe と
  updater.bat、Windows のソケットの挙動（止めた直後の同じポートへの bind、
  `SO_LINGER` の構造体の形など）、テキストモードで書く改行（CRLF）、pyftpdlib が
  Linux では `os.sendfile` を使うこと。コンテナの事情では、root で動くので
  読み取り専用のファイルにも書けること、IPv6 が無効なこと。
- 落ちたテストが自分の変更のせいか迷ったら、変更前のコミットでも同じく
  落ちるかを確かめる。
