"""接続ワーカースレッドが GUI ウィジェットを直接操作しないことを確認する。

Qt のウィジェットを GUI スレッド外から触るのは未定義動作で、
クラッシュや表示崩れの原因になる。接続処理は SSH / シリアル / Telnet の
3種がほぼ同一のコピーで、同じ誤りが3箇所へ転記されていた。

接続そのものは実機が要るためテストできないので、ここでは
「ワーカースレッドの中でウィジェットを直接呼んでいないこと」を構造として固定する。
"""
import ast
import io
import sys
import unittest

sys.path.insert(0, "src")

SOURCE = "src/ui/main_window.py"

# ワーカースレッドの中で呼んではいけないもの（GUI スレッド専用）
FORBIDDEN_ATTRS = {"append_output", "setText", "addTab", "removeTab", "setCurrentIndex"}


def _thread_target_functions(tree):
    """threading.Thread(target=X) の X として渡される関数定義を集める。"""
    targets = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = getattr(func, "attr", getattr(func, "id", None))
        if name != "Thread":
            continue
        for kw in node.keywords:
            if kw.arg == "target" and isinstance(kw.value, ast.Name):
                targets.add(kw.value.id)
    return targets


class ConnectThreadSafetyTest(unittest.TestCase):
    def setUp(self):
        self.src = io.open(SOURCE, encoding="utf-8").read()
        self.tree = ast.parse(self.src)

    def test_worker_threads_exist(self):
        """前提: 接続処理はワーカースレッドで動いている。"""
        self.assertTrue(_thread_target_functions(self.tree),
                        "threading.Thread(target=...) が見つからない")

    def test_worker_threads_do_not_touch_widgets(self):
        names = _thread_target_functions(self.tree)
        offenders = []

        for node in ast.walk(self.tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in names:
                continue
            for inner in ast.walk(node):
                if not isinstance(inner, ast.Call):
                    continue
                f = inner.func
                if isinstance(f, ast.Attribute) and f.attr in FORBIDDEN_ATTRS:
                    offenders.append("%s() 内の %d行目: .%s()"
                                     % (node.name, inner.lineno, f.attr))

        self.assertEqual(
            offenders, [],
            "ワーカースレッドから GUI ウィジェットを直接操作している:\n  "
            + "\n  ".join(offenders))

    def test_connection_failure_goes_through_signal(self):
        """接続失敗の通知が3種ともシグナル経由であること。

        見ているのはスレッドの安全性であって文言ではないので、
        改行の書き方 (端末向けの CRLF) には依存させない。
        """
        import re

        through_signal = re.findall(
            r'\.output_received\.emit\("[\\rn]*接続失敗', self.src)
        self.assertEqual(
            len(through_signal), 3,
            "接続失敗の通知がシグナル経由になっていない")
        direct = re.findall(
            r'terminal_widget\.\w+\([^)]*接続失敗', self.src)
        self.assertEqual(
            direct, [], "ウィジェットの直接呼び出しが残っている")


if __name__ == "__main__":
    unittest.main()
