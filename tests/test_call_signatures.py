"""呼び出し側の引数が、実際のシグネチャと噛み合っていることを機械的に確かめる。

「呼び出し側にだけキーワード引数を足して、コンストラクタに入れ忘れる」種類の
配線ミスは実際に起きた（DownloadThread の sha256_url）。単体テストは対象を
直接叩くため素通りし、UI を実行するまで気づけなかった。

ここでは src/ 全体を AST で読み、リポジトリ内で定義されたクラス・関数への
呼び出しについて、渡しているキーワードが定義側に存在するかを照合する。
文字列検索ではなく構文木で見るので、「呼び出し側を消しても緑のまま」に
ならない。
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, "src")

SRC = "src"


def _iter_sources():
    for root, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in sorted(files):
            if fn.endswith(".py"):
                path = os.path.join(root, fn)
                with open(path, encoding="utf-8") as f:
                    yield path, ast.parse(f.read(), filename=path)


def _params(func_node):
    """関数定義から、受け取れる引数名の集合と **kwargs の有無を返す。"""
    a = func_node.args
    names = {p.arg for p in a.posonlyargs + a.args + a.kwonlyargs}
    return names, a.kwarg is not None


class CallSignatureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # クラス名 -> (定義ファイル, __init__ の引数集合, **kwargs の有無)
        cls.classes = {}
        # 同名が複数あるものは判定できないので除外する
        dupes = set()
        cls.trees = list(_iter_sources())

        for path, tree in cls.trees:
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                init = next((n for n in node.body
                             if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
                if init is None:
                    continue
                if node.name in cls.classes:
                    dupes.add(node.name)
                    continue
                names, has_kwargs = _params(init)
                cls.classes[node.name] = (path, names - {"self"}, has_kwargs)
        for d in dupes:
            cls.classes.pop(d, None)
        cls.ambiguous = dupes

    def test_repository_has_classes_to_check(self):
        """前提: 検査対象のクラスが集まっていること（空振りテストにしない）。"""
        self.assertGreater(len(self.classes), 10,
                           "検査対象のクラスが集まっていない: %d" % len(self.classes))

    def test_constructor_keywords_exist_in_signature(self):
        """クラスを名前で呼び出している箇所のキーワードが、定義側に存在すること。"""
        offenders = []
        for path, tree in self.trees:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Name):
                    continue
                info = self.classes.get(node.func.id)
                if info is None:
                    continue
                _def_path, allowed, has_kwargs = info
                if has_kwargs:
                    continue
                for kw in node.keywords:
                    if kw.arg is None:      # **something は判定不能
                        continue
                    if kw.arg not in allowed:
                        offenders.append(
                            "%s:%d %s(%s=...) — %s の __init__ は %s を受け取らない"
                            % (path, node.lineno, node.func.id, kw.arg,
                               node.func.id, kw.arg))

        self.assertEqual(
            offenders, [],
            "呼び出し側のキーワードが定義側に存在しない:\n  " + "\n  ".join(offenders))

    def test_constructor_positional_count_fits(self):
        """位置引数の個数が、定義側の受け取れる数を超えていないこと。"""
        offenders = []
        limits = {}
        for path, tree in self.trees:
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    init = next((n for n in node.body
                                 if isinstance(n, ast.FunctionDef) and n.name == "__init__"), None)
                    if init and node.name in self.classes:
                        a = init.args
                        limits[node.name] = (len(a.posonlyargs) + len(a.args) - 1,
                                             a.vararg is not None)

        for path, tree in self.trees:
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                lim = limits.get(node.func.id)
                if lim is None:
                    continue
                max_pos, has_vararg = lim
                if has_vararg:
                    continue
                given = len([a for a in node.args if not isinstance(a, ast.Starred)])
                if any(isinstance(a, ast.Starred) for a in node.args):
                    continue
                if given > max_pos:
                    offenders.append(
                        "%s:%d %s に位置引数 %d 個（定義は最大 %d 個）"
                        % (path, node.lineno, node.func.id, given, max_pos))

        self.assertEqual(offenders, [],
                         "位置引数が多すぎる呼び出し:\n  " + "\n  ".join(offenders))

    def test_self_method_keywords_exist(self):
        """self.method(kw=...) のキーワードが、同じクラスの定義に存在すること。"""
        offenders = []
        for path, tree in self.trees:
            for cls_node in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
                methods = {n.name: _params(n) for n in cls_node.body
                           if isinstance(n, ast.FunctionDef)}
                for node in ast.walk(cls_node):
                    if not isinstance(node, ast.Call):
                        continue
                    f = node.func
                    if not (isinstance(f, ast.Attribute)
                            and isinstance(f.value, ast.Name) and f.value.id == "self"):
                        continue
                    info = methods.get(f.attr)
                    if info is None:
                        continue
                    allowed, has_kwargs = info
                    if has_kwargs:
                        continue
                    for kw in node.keywords:
                        if kw.arg is None:
                            continue
                        if kw.arg not in allowed:
                            offenders.append(
                                "%s:%d self.%s(%s=...) — 定義は %s を受け取らない"
                                % (path, node.lineno, f.attr, kw.arg, kw.arg))

        self.assertEqual(offenders, [],
                         "self 経由の呼び出しで、定義に無いキーワードを渡している:\n  "
                         + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
