#!/usr/bin/env bash
# gVisor 层验收入口。真正的检查逻辑在 verify_gvisor.py 里，原因见该文件开头：
# 用 Python 是因为「命令是否缺失」必须能被判定成一个结论，
# 而 bash 里 `cmd && fail || pass` 在命令缺失时会静默落到 pass ——
# 那正是上一版把「没测」说成「没问题」的原因。
#
# 退出码原样透传：0=全部 PASS；1=有 FAIL；2=有 BLOCKED/UNKNOWN。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
PY=${PY:-python3}

if ! command -v "$PY" >/dev/null 2>&1; then
  printf "%s\n" "找不到 $PY：无法进行任何检查（BLOCKED）" >&2
  exit 2
fi

cd "$ROOT" || exit 2
PYTHONPATH=src exec "$PY" deploy/gvisor/verify_gvisor.py "$@"