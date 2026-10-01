"""SNMP の GET/WALK のテストのうち、不具合が再発するとワーカーが止まったまま残るものを、子プロセスで動かす。

何が起きていたか: v1 の機器が Integer32 の範囲を超える INTEGER（2147483648）を
返すと、pysnmp 7.1.28 は応答を待ちの一覧から外したあとの v1→v2 の変換で例外を
出し、例外はイベントループのコールバックの中で止まる。直す前は GET/WALK が
終わらず、取り消し（request_cancel）も効かなかった（snmp_manager の
_LoopFailure の docstring）。これを捕まえるテスト（tests/test_snmp_loopback_get_walk.py
の LoopbackUnconvertibleV1ValueTest の 3 件と、LoopbackResourceTest の
test_an_unconvertible_value_releases_everything）は、同じプロセスで動かして
いると、不具合が再発したとき制限時間で失敗はするが、止まったワーカー
（QThread）・イベントループ・ソケットが pytest のプロセスに残る。取り消しも
効かないので片付ける手段が無く、後続のテストへ影響したり、終了時に Qt が
「QThread: Destroyed while thread is still running」で異常終了したりしうる。

どう直したか: そのテストの中身（相手役を立て、SNMPManager で GET/WALK を行う）を
このファイルのシナリオにして、子プロセスで動かす。子は確かめる材料（結果・
かかった秒数・partial・cancelled・エラーの文言、資源のテストなら閉じた
ループ・ソケットの数など）を、印を付けた 1 行の JSON（ASCII）で標準出力へ
返し、os._exit で終わる。止まったワーカーが残っていても、プロセスごと消える。
判定は親（テスト）が、同じプロセスで動かしていたときと同じ条件で行う。

親の側（run_in_child）:
  - sys.executable で子を起動する（QT_QPA_PLATFORM=offscreen、
    PYTHONIOENCODING=utf-8、作業フォルダはリポジトリの直下）。子は src と
    tests をこのファイルの場所から探すので、起動したときの作業フォルダに
    よらない。
  - 子の標準出力・標準エラーは一時ファイルで受ける。パイプだと、子の子
    （下の venv の中継）がパイプを持ったまま残ったとき、読み終わりを
    待ち続ける。
  - KILL_AFTER 秒で終わらなければ、taskkill /F /T で子を子孫ごと止め、
    TerminateProcess（Popen.kill）も行って失敗にする。venv の python.exe は
    本物のインタープリタを子として起動する中継なので、子孫ごと止める。
  - 時間内に終わっても、終了コード（0）と結果の行（ちょうど 1 行・
    子の中の失敗が無いこと）を確かめる。

子の側（python snmp_isolated_scenarios.py <シナリオの名前>）:
  - 操作ごとの待ちは元のテストと同じ（30 秒。ManagerRecorder.wait は、その
    あと取り消しを頼んで 15 秒待つ）。終わらなければ、その AssertionError を
    子の中の失敗として JSON で返す。
  - CHILD_GIVE_UP_AFTER 秒たっても終わらなければ、全スレッドのスタックを
    標準エラーへ出して自分で終わる（faulthandler）。親の kill は、その
    さらに外側の備え。
  - 材料は JSON へそのまま書く。JSON にできない値（例外のオブジェクトなど）
    があっても文字列に変えず、終了コード 2・結果の行 0 行で終わる（親が
    失敗にする）。文字列に変えると、親の型の確かめをすり抜ける。

通信は 127.0.0.1 だけ。
"""
import asyncio
import faulthandler
import gc
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import traceback

import snmp_loopback_agent as loopback
from pyasn1.type import univ
from pysnmp.proto import rfc1902

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(REPO_ROOT, "src")
SCRIPT = os.path.abspath(__file__)

# 子が結果を返す行の印（この印で始まる行の残りが JSON）
RESULT_MARKER = "NETBELT-SNMP-SCENARIO-RESULT "
# Integer32 の範囲を超える v1 の INTEGER（最大値 + 1）
BIG = 2147483648
# v1 の GET/WALK の引数（test_snmp_loopback_get_walk.py の PROFILES[0][1] と同じ）
V1 = {"version": "v1", "community": "public"}
# 操作ごとの待ちの上限（元のテストの recorder.run(..., timeout=30) と同じ）
OPERATION_TIMEOUT = 30
# 子が自分で諦めるまでの秒数と、親が子を止めるまでの秒数。通常は数秒で終わる。
# 子の中の待ちは、最長で 30 秒 + 取り消しの 15 秒 + 起動（遅いランナーで
# 十数秒）。子が終わらない理由を自分で返せるよう、その外側に置く
CHILD_GIVE_UP_AFTER = 75
KILL_AFTER = 90


# --- 相手役と、閉じたかどうかの材料（同じプロセスのテストも使う） ---

class RawV1Agent:
    """SNMPv1 の要求に、決めた値を正しい request-id・community で返す相手（生の UDP）。

    pysnmp の応答側は Integer32 の範囲を超える INTEGER を返せないので、
    v1 の PDU を pysnmp.proto.api で組んで直接返す。rows は OID の昇順の
    [(OID の組, 値)]。GET はその OID の値を、GETNEXT は次の行を返す。
    """

    def __init__(self, rows):
        from pysnmp.proto import api
        self._api = api.PROTOCOL_MODULES[api.SNMP_VERSION_1]
        self.rows = list(rows)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((loopback.HOST, 0))
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="snmp-raw-v1-agent")
        self._thread.start()

    def _answer(self, oid, next_):
        for name, value in self.rows:
            if (name > oid) if next_ else (name == oid):
                return name, value
        return oid, rfc1902.Null("")

    def _run(self):
        from pyasn1.codec.ber import decoder, encoder
        mod = self._api
        while not self._stop.is_set():
            try:
                data, address = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            request, _rest = decoder.decode(data, asn1Spec=mod.Message())
            response = mod.apiMessage.get_response(request)
            pdu = mod.apiMessage.get_pdu(request)
            next_ = pdu.tagSet == mod.GetNextRequestPDU.tagSet
            mod.apiPDU.set_varbinds(
                mod.apiMessage.get_pdu(response),
                [self._answer(tuple(oid), next_)
                 for oid, _value in mod.apiPDU.get_varbinds(pdu)])
            self._sock.sendto(encoder.encode(response), address)

    def close(self):
        self._stop.set()
        self._thread.join(5)
        self._sock.close()


def open_loops():
    """プロセス内の、閉じていないイベントループ（id の集合）"""
    return {id(obj) for obj in gc.get_objects()
            if isinstance(obj, asyncio.AbstractEventLoop) and not obj.is_closed()}


class LoopSpy:
    """core.snmp_manager._new_event_loop の代わり。作ったループと、その上で開いた UDP のソケットを記録する。

    ループは本物（元の _new_event_loop）を使い、そのループの
    create_datagram_endpoint だけを包んで、pysnmp が開いたソケットを覚える
    （テストの中で閉じる差し替え。snmp_manager の後始末には手を入れない）。
    """

    def __init__(self, original):
        self._original = original
        self._lock = threading.Lock()
        self.loops = []
        self.sockets = []

    def __call__(self):
        loop = self._original()
        create = loop.create_datagram_endpoint

        async def create_and_record(*args, **kwargs):
            transport, protocol = await create(*args, **kwargs)
            with self._lock:
                self.sockets.append(transport.get_extra_info("socket"))
            return transport, protocol

        loop.create_datagram_endpoint = create_and_record
        with self._lock:
            self.loops.append(loop)
        return loop


def released_facts(spy, main_loop, loops_before, threads_before):
    """GET/WALK が作ったループ・ソケット・スレッドが閉じたかどうかの材料（JSON にできる形）。

    判定は test_snmp_loopback_get_walk.py の
    LoopbackResourceTest._assert_released_facts が行う。同じプロセスで動かす
    テストと子プロセスのシナリオが、同じ材料を同じ順で集めるよう、ここに
    1 つだけ置く。
    """
    from PyQt6.QtWidgets import QApplication
    facts = {
        "loops": len(spy.loops),
        "sockets": len(spy.sockets),
        "loops_running": [loop.is_running() for loop in spy.loops],
        "loops_closed": [loop.is_closed() for loop in spy.loops],
        "socket_filenos": [sock.fileno() for sock in spy.sockets],
        "main_loop_kept": (asyncio.get_event_loop_policy().get_event_loop()
                           is main_loop),
        "main_loop_closed": main_loop.is_closed(),
    }
    QApplication.processEvents()
    gc.collect()
    facts["leftover_loops"] = sorted(open_loops() - loops_before)
    # QThread（ワーカー）の中で threading.Thread を作ると、そのスレッドの
    # 代わりの _DummyThread が登録されて消えない（Python 3.11 の threading）。
    # ワーカーが終わったことは recorder.wait() が finished で確かめている
    facts["new_threads"] = [t.name for t in threading.enumerate()
                            if t not in threads_before
                            and not isinstance(t, threading._DummyThread)]
    return facts


# --- 記録の受け渡し ---

def recorder_snapshot(recorder):
    """ManagerRecorder の記録を、JSON にできる形にする"""
    return {
        "completed": [[ok, result] for ok, result in recorder.completed],
        "partial": list(recorder.partial),
        "cancelled": list(recorder.cancelled),
        "progress": list(recorder.progress),
        "errors": list(recorder.errors),
    }


def _as_rows(value):
    """JSON で list になった行（OID, 型名, 値）を tuple に戻す。文字列はそのまま"""
    if isinstance(value, list):
        return [tuple(row) if isinstance(row, list) else row for row in value]
    return value


class ReportedRecorder:
    """子プロセスが返した ManagerRecorder の記録。親が同じ条件で判定するのに使う。

    属性は ManagerRecorder と同じ名前・同じ形（行は tuple）。result() は
    ManagerRecorder.result そのもの（operation_completed がちょうど 1 回
    出たことを確かめて (ok, 結果) を返す）。
    """

    def __init__(self, snapshot):
        self.completed = [(ok, _as_rows(result))
                          for ok, result in snapshot["completed"]]
        self.partial = list(snapshot["partial"])
        self.cancelled = [_as_rows(rows) for rows in snapshot["cancelled"]]
        self.progress = list(snapshot["progress"])
        self.errors = list(snapshot["errors"])

    def result(self):
        return loopback.ManagerRecorder.result(self)


# --- 親の側 ---

def _kill_tree(proc):
    """子を子孫ごと止め（taskkill /F /T）、TerminateProcess も行う。終了コードを返す"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass    # taskkill が無い・終わらない。下の TerminateProcess だけになる
    proc.kill()
    try:
        return proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        return None


def run_in_child(name):
    """シナリオ name を子プロセスで動かし、子が返した記録（dict）を返す。

    KILL_AFTER 秒で終わらない（子を止める）・結果の行がちょうど 1 行でない・
    子の中で失敗した・終了コードが 0 でない、のどれでも AssertionError に
    する。メッセージには子の標準出力・標準エラーの末尾を付ける。
    """
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    # 子の出力は UTF-8 で読むので、書く側もそろえる。固定しないと子は
    # ロケールのコードページで書き、英語版 Windows（GitHub のランナーは
    # cp1252）では製品の日本語の print が UnicodeEncodeError になる。
    # 結果の行は ASCII の JSON なので、どちらでも読める
    env["PYTHONIOENCODING"] = "utf-8"
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        # -u: 止めたときも、それまでの出力をファイルに残す
        proc = subprocess.Popen([sys.executable, "-u", SCRIPT, name],
                                stdin=subprocess.DEVNULL, stdout=out,
                                stderr=err, env=env, cwd=REPO_ROOT)
        timed_out = False
        try:
            code = proc.wait(timeout=KILL_AFTER)
        except subprocess.TimeoutExpired:
            timed_out = True
            code = _kill_tree(proc)
        except BaseException:
            # 中断（Ctrl+C など）でも子を残さない
            _kill_tree(proc)
            raise
        out.seek(0)
        err.seek(0)
        stdout = out.read().decode("utf-8", "replace")
        stderr = err.read().decode("utf-8", "replace")
    lines = [line[len(RESULT_MARKER):] for line in stdout.splitlines()
             if line.startswith(RESULT_MARKER)]
    # 結果の行は、中身（子の中の失敗など）を別に示すので末尾には入れない
    printed = "\n".join(line for line in stdout.splitlines()
                        if not line.startswith(RESULT_MARKER))
    tails = ("\n--- 子の標準出力の末尾 ---\n%s\n--- 子の標準エラーの末尾 ---\n%s"
             % (printed[-2000:], stderr[-4000:]))
    if timed_out:
        raise AssertionError(
            "子プロセスのシナリオ %s が %d 秒で終わらないので止めた（終了コード %s）%s"
            % (name, KILL_AFTER, code, tails))
    if len(lines) != 1:
        raise AssertionError(
            "子プロセスのシナリオ %s の結果の行が %d 行（1 行のはず。終了コード %s）%s"
            % (name, len(lines), code, tails))
    report = json.loads(lines[0])
    if "error" in report:
        raise AssertionError("子プロセスのシナリオ %s の中で失敗した:\n%s%s"
                             % (name, report["error"], tails))
    if code != 0:
        raise AssertionError("子プロセスのシナリオ %s の終了コードが %s%s"
                             % (name, code, tails))
    return report


# --- 子の側（シナリオ。元のテストの本体と同じ手順） ---

# 子の中で作った SNMPManager と QApplication は、終わるまで持つ。手放すと、
# 止まったワーカー（実行中の QThread）が破棄され、結果を返す前に Qt が
# 異常終了する
_KEEP = []


def _recorder():
    from core.snmp_manager import SNMPManager
    manager = SNMPManager()
    _KEEP.append(manager)
    return loopback.ManagerRecorder(manager)


def _get(recorder, port, oids):
    return recorder.run(lambda: recorder.manager.snmp_get(
        loopback.HOST, list(oids), port=port, **V1), timeout=OPERATION_TIMEOUT)


def _walk(recorder, port, oid):
    return recorder.run(lambda: recorder.manager.snmp_walk(
        loopback.HOST, oid, port=port, **V1), timeout=OPERATION_TIMEOUT)


def _unconvertible_value_fails_the_get(report):
    oid = loopback.TYPED_SUBTREE + (1, 0)
    agent = RawV1Agent([(oid, univ.Integer(BIG))])
    try:
        recorder = _recorder()
        report["elapsed"] = _get(recorder, agent.port, [loopback.oid_text(oid)])
        report["recorder"] = recorder_snapshot(recorder)
    finally:
        agent.close()


def _unconvertible_value_ends_the_walk(report):
    base = loopback.TYPED_SUBTREE
    agent = RawV1Agent([
        (base + (1, 0), univ.Integer(1)),
        (base + (2, 0), univ.Integer(2)),
        (base + (3, 0), univ.Integer(BIG)),
    ])
    try:
        recorder = _recorder()
        report["elapsed"] = _walk(recorder, agent.port, loopback.oid_text(base))
        report["recorder"] = recorder_snapshot(recorder)
    finally:
        agent.close()


def _unconvertible_first_value_fails_the_walk(report):
    base = loopback.TYPED_SUBTREE
    agent = RawV1Agent([(base + (1, 0), univ.Integer(BIG))])
    try:
        recorder = _recorder()
        report["elapsed"] = _walk(recorder, agent.port, loopback.oid_text(base))
        report["recorder"] = recorder_snapshot(recorder)
    finally:
        agent.close()


def _unconvertible_value_releases_everything(report):
    """LoopbackResourceTest の setUp と、元のテストの本体と同じ手順"""
    from unittest import mock
    import core.snmp_manager as snmp_manager
    spy = LoopSpy(snmp_manager._new_event_loop)
    patch = mock.patch.object(snmp_manager, "_new_event_loop", spy)
    patch.start()
    # メインスレッドのループを目印のループにしておく（置き換えられたら分かる）
    main_loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(main_loop)
    agent = None
    try:
        base = loopback.TYPED_SUBTREE
        agent = RawV1Agent([(base + (1, 0), univ.Integer(1)),
                            (base + (2, 0), univ.Integer(BIG))])
        # 相手のスレッド（最後まで動いている）は数えない
        threads_before = set(threading.enumerate())
        loops_before = open_loops()
        recorder = _recorder()
        _get(recorder, agent.port, [loopback.oid_text(base + (2, 0))])
        report["get"] = recorder_snapshot(recorder)
        _walk(recorder, agent.port, loopback.oid_text(base))
        report["walk"] = recorder_snapshot(recorder)
        report["released"] = released_facts(spy, main_loop, loops_before,
                                            threads_before)
    finally:
        if agent is not None:
            agent.close()
        patch.stop()
        main_loop.close()
        asyncio.set_event_loop(None)


# シナリオの名前 → 本体（名前は test_snmp_loopback_get_walk.py のテストの名前から
# test_ を除いたもの）
SCENARIOS = {
    "an_unconvertible_value_fails_the_get_at_once":
        _unconvertible_value_fails_the_get,
    "an_unconvertible_value_ends_the_walk_with_the_rows_so_far":
        _unconvertible_value_ends_the_walk,
    "an_unconvertible_first_value_fails_the_walk":
        _unconvertible_first_value_fails_the_walk,
    "an_unconvertible_value_releases_everything":
        _unconvertible_value_releases_everything,
}


def _emit(report):
    # default は付けない。JSON にできない値（例外のオブジェクトなど）を repr の
    # 文字列に変えて返すと、親の型の確かめ（assertFailed の
    # assertIsInstance(payload, str) など）をすり抜け、同じプロセスで動かして
    # いたときより確かめが弱くなる。JSON にできなければ TypeError になり、
    # main が終了コード 2・結果の行 0 行で終わるので、親が失敗にする
    line = RESULT_MARKER + json.dumps(report, ensure_ascii=True)
    # 製品の print（ワーカーのスレッドから出る）と同じ行に混ざらないよう、
    # 改行してから書く
    sys.stdout.write("\n" + line + "\n")
    sys.stdout.flush()


def main(argv):
    """子プロセスの入口。シナリオを 1 つ動かし、結果の行を書いて os._exit で終わる"""
    faulthandler.dump_traceback_later(CHILD_GIVE_UP_AFTER, exit=True)
    name = argv[1] if len(argv) > 1 else ""
    report = {"scenario": name}
    code = 0
    try:
        scenario = SCENARIOS[name]
        sys.path.insert(0, SRC_DIR)
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt6.QtWidgets import QApplication
        _KEEP.append(QApplication.instance() or QApplication([]))
        import core.snmp_manager as snmp_manager
        # 親は、子が自分と同じ src を読んだことを確かめる
        report["snmp_manager"] = os.path.abspath(snmp_manager.__file__)
        scenario(report)
    except BaseException:
        report["error"] = traceback.format_exc()
        code = 1
    faulthandler.cancel_dump_traceback_later()
    try:
        _emit(report)
    except BaseException:
        # 結果を JSON にできない。子の中の失敗も結果の行では返せないので、
        # 標準エラーへ出す（親は標準エラーの末尾を失敗のメッセージに付ける）
        traceback.print_exc()
        if "error" in report:
            sys.stderr.write(report["error"])
        code = 2
    sys.stderr.flush()
    # 止まったワーカーが残っていても待たずに終わる。普通に終えると、Qt が
    # 実行中の QThread を破棄して異常終了するか、後始末で止まりうる
    os._exit(code)


if __name__ == "__main__":
    main(sys.argv)
