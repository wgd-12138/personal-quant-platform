#!/usr/bin/env bash
# 云端环境探测：把 gVisor 可用性相关的全部事实量出来，输出 JSON。
#
# 为什么不是「uname -r 看一下就下结论」：
#   内核版本说明不了 runsc 是否安装、是否被 Docker 注册、/dev/kvm 是否可读。
#   这几条任意一条不满足，这一层就不可用，而且原因必须能原样打印出来。
#
# 本脚本复用 pq_agent.sandbox.gvisor_runtime.probe_host，保证「探测」和「运行时判定」
# 用的是同一份逻辑，不会出现「脚本说行、运行时说不行」的分叉。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
PY=${PY:-python3}

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 python3，探测无法进行" >&2
  exit 127
fi

cd "$ROOT" || exit 1
PYTHONPATH=src "$PY" -c "import json; from pq_agent.sandbox.gvisor_runtime import probe_host, decide_platform, platform_blocker, platform_note; f=probe_host(); print(json.dumps({'facts': f.to_dict(), 'platform': decide_platform(f), 'blocker': platform_blocker(f), 'note': platform_note(f)}, ensure_ascii=False, indent=2))"
