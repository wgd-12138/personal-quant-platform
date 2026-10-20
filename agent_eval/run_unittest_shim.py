"""在测试进程内调整临时目录权限，供受限 Windows 沙箱运行完整单测。

只将 os.mkdir(mode=0o700) 映射为 0o750；不修改产品代码，也不改测试断言。
普通开发环境可以直接运行 unittest，无需使用本脚本。
"""
import os
import sys
import unittest

_orig_mkdir = os.mkdir


def _mkdir(path, mode=0o777, *args, **kwargs):
    if mode == 0o700:
        mode = 0o750
    return _orig_mkdir(path, mode, *args, **kwargs)


os.mkdir = _mkdir

if __name__ == "__main__":
    suite = unittest.TestLoader().discover("tests")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
