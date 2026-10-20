"""AST 静态拒绝层：快速挡掉最明显的危险写法。

必须说清楚它不是什么：它是一张黑名单，只能拦「照着教科书写的」危险代码。
字符串拼接、getattr 拼名字、编码后用 exec 解回来等写法都能绕过。
所以本层的产出只有两种含义：
  - 命中：不用执行，直接拒绝（这一步是确定有效的）；
  - 未命中：不代表安全，只代表「静态没看出来」。
上层绝不允许把「静态通过」当成「安全」。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any

# 危险模块：导入即拒绝
DENY_MODULES: tuple[str, ...] = (
    "ctypes", "socket", "subprocess", "shutil", "winreg", "win32api", "win32con",
    "multiprocessing", "signal", "pty", "pickle", "marshal", "shelve", "importlib",
    "urllib", "http", "ftplib", "smtplib", "telnetlib", "xmlrpc", "requests", "httpx",
    "aiohttp", "asyncio", "webbrowser", "platform", "getpass", "glob", "tempfile",
    "pathlib", "os",
)

# sys 单独放行，但只放行「读受控 stdin / stdout」这一条路：
# 契约要求沙箱支持向子进程喂受控数据，而读 stdin 只能通过 sys.stdin。
# sys 的其余危险面（sys.modules、sys.path、sys.argv、__import__ 逃逸）
# 已经在 DENY_ATTRS 和 DENY_CALL_NAMES 里逐条挡住。
# 注意：stdin / stdout / stderr 必须放行 —— 契约要求沙箱能喂受控数据并收回输出，
# 这三个流就是唯一的通道。把它们挡住等于把 sys 白放行了。
DENY_SYS_ATTRS: tuple[str, ...] = (
    "modules", "path", "argv", "meta_path", "path_hooks", "setrecursionlimit",
    "settrace", "setprofile", "addaudithook", "executable", "prefix", "base_prefix",
    "dont_write_bytecode", "__stdin__", "__stdout__", "__stderr__",
)

# 危险函数名（按名字匹配，不看来自哪个模块）
DENY_CALL_NAMES: tuple[str, ...] = (
    "eval", "exec", "compile", "__import__", "open", "input",
    "system", "popen", "spawn", "spawnl", "spawnv", "fork", "kill", "exit", "_exit",
    "remove", "unlink", "rmdir", "rmtree", "chmod", "chown", "rename", "replace",
    "getattr", "setattr", "delattr", "globals", "locals", "vars", "dir",
    "connect", "send", "recv", "urlopen", "run", "Popen", "check_output",
)

# 危险属性名：对象逃逸的经典入口
DENY_ATTRS: tuple[str, ...] = (
    "__subclasses__", "__bases__", "__mro__", "__globals__", "__code__",
    "__builtins__", "__dict__", "__class__", "__reduce__", "__reduce_ex__",
    "__getattribute__", "__getattr__", "f_globals", "f_builtins", "gi_frame",
    "cr_frame", "ag_frame", "mro",
)

# 这些名字作为纯赋值目标是允许的（比如 x.__class__ 里的 x 本身没问题，
# 真正危险的是取到属性；这里列出的是被访问时会拒绝的属性，见 DENY_ATTRS）


@dataclass(frozen=True)
class StaticFinding:
    """一条静态命中。"""

    kind: str
    target: str
    line: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "line": self.line, "detail": self.detail}

    def __str__(self) -> str:
        return "%s:%s(line %d)" % (self.kind, self.target, self.line)


@dataclass
class StaticReport:
    """静态检查报告。clean 只代表「静态没看出来」。"""

    findings: list[StaticFinding] = field(default_factory=list)
    syntax_error: str = ""
    node_count: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings and not self.syntax_error

    @property
    def blocked(self) -> bool:
        return bool(self.findings) or bool(self.syntax_error)

    def to_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "syntax_error": self.syntax_error,
            "node_count": self.node_count,
            "findings": [f.to_dict() for f in self.findings],
        }


def _root_name(node: ast.AST) -> str:
    """取属性链最左边的名字，例如 a.b.c 取 a。"""
    cur = node
    while isinstance(cur, ast.Attribute):
        cur = cur.value
    if isinstance(cur, ast.Name):
        return cur.id
    return ""


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[StaticFinding] = []
        self.node_count = 0

    def _add(self, kind: str, target: str, node: ast.AST, detail: str) -> None:
        self.findings.append(
            StaticFinding(kind=kind, target=target, line=getattr(node, "lineno", 0), detail=detail)
        )

    def generic_visit(self, node: ast.AST) -> None:
        self.node_count += 1
        super().generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in DENY_MODULES:
                self._add("import", top, node, "禁止导入模块 %s" % top)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        top = (node.module or "").split(".")[0]
        if top in DENY_MODULES:
            self._add("import", top, node, "禁止从模块 %s 导入" % top)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in DENY_CALL_NAMES:
                self._add("call", func.id, node, "禁止调用 %s" % func.id)
        elif isinstance(func, ast.Attribute):
            if func.attr in DENY_CALL_NAMES:
                self._add("call", func.attr, node, "禁止调用属性方法 %s" % func.attr)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in DENY_ATTRS:
            self._add("attribute", node.attr, node, "禁止访问属性 %s（对象逃逸经典入口）" % node.attr)
        elif node.attr in DENY_SYS_ATTRS and _root_name(node) == "sys":
            # sys 放行只为一件事：读受控 stdin。其余 sys 属性逐个挡掉。
            self._add("attribute", "sys.%s" % node.attr, node, "禁止访问 sys.%s" % node.attr)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in ("__builtins__", "__loader__", "__spec__"):
            self._add("name", node.id, node, "禁止直接引用 %s" % node.id)
        self.generic_visit(node)


def analyze(code: str) -> StaticReport:
    """解析并静态检查一段源码。语法错误也算阻断。"""
    report = StaticReport()
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        report.syntax_error = "%s (line %s)" % (exc.msg, exc.lineno)
        return report
    visitor = _Visitor()
    visitor.visit(tree)
    report.findings = visitor.findings
    report.node_count = visitor.node_count
    return report


def is_clean(code: str) -> bool:
    """方便调用，但调用方必须知道 clean 不等于安全。"""
    return analyze(code).clean
