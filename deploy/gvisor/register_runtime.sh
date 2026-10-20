#!/usr/bin/env bash
# 把 runsc 注册进 Docker 的 runtime 列表，然后重启 Docker。
#
# 关键取舍：只加 runtimes，**不改 default-runtime**。
# 把 runsc 设成默认会让机器上所有容器一起换内核（构建、监控、其他服务都受影响），
# 而我们的决定只是「agent 的代码执行这一路走 runsc」。
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
DAEMON_JSON=/etc/docker/daemon.json

if ! command -v runsc >/dev/null 2>&1; then
  echo "没有 runsc。先执行 install_runsc.sh" >&2
  exit 1
fi

if [ ! -f "$DAEMON_JSON" ]; then
  echo "{}" > "$DAEMON_JSON"
fi
cp "$DAEMON_JSON" "$DAEMON_JSON.bak.$(date +%Y%m%d%H%M%S)"

# 用 python3 合并 JSON：不动别人已经写进去的字段。
python3 - "$DAEMON_JSON" "$HERE/daemon.json.example" <<PY
import json, sys
target, example_path = sys.argv[1], sys.argv[2]
current = json.load(open(target, encoding='utf-8'))
example = json.load(open(example_path, encoding='utf-8'))
runtimes = dict(current.get('runtimes') or {})
runtimes.update(example.get('runtimes') or {})
current['runtimes'] = runtimes
json.dump(current, open(target, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('runtimes =', sorted(runtimes))
PY

echo "重启 Docker 以加载新 runtime"
systemctl restart docker

echo "确认注册结果："
docker info --format "{{json .Runtimes}}"

echo "确认 runsc 版本："
runsc --version
