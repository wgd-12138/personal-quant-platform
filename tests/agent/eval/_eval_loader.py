"""按路径加载评测脚本（agent_eval 不是 Python 包，没有 __init__.py）。

按路径加载时必须先把模块塞进 sys.modules：dataclasses 在执行装饰器时会读
module.__dict__，不注册就拿到 None，报 AttributeError: NoneType object has no attribute __dict__。
"""
import importlib.util
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def load_eval_module(name: str, filename: str | None = None):
    src = REPO_ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    path = REPO_ROOT / "agent_eval" / (filename or (name + ".py"))
    spec = importlib.util.spec_from_file_location("pq_eval_" + name, path)
    if spec is None or spec.loader is None:
        raise ImportError("无法按路径加载评测模块：%s" % path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
