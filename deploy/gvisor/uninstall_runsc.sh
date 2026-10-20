#!/usr/bin/env bash
# 回滚：停用 gVisor，回到 runc。不删用户数据、不删镜像。
#
# 顺序很重要：先把 agent 侧的开关关掉，再摘 Docker 的 runtime，
# 否则会出现「agent 以为在用 runsc、Docker 上已经没有 runsc」的窗口。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
DAEMON_JSON=/etc/docker/daemon.json

echo "[1/3] 关闭 agent 侧开关（把 PQ_AGENT_SANDBOX_RUNTIME 设为 local 或直接删除）"
echo "  本次进程会话内："
echo "    export PQ_AGENT_SANDBOX_RUNTIME=local"
echo "  永久生效请自己决定写进哪里；本脚本不替你改系统环境变量。"

echo "[2/3] 从 daemon.json 摘掉 runsc（保留其他 runtime，保留备份）"
if [ -f "$DAEMON_JSON" ]; then
  cp "$DAEMON_JSON" "$DAEMON_JSON.bak.rollback.$(date +%Y%m%d%H%M%S)"
  python3 - "$DAEMON_JSON" <<PY
import json, sys
target = sys.argv[1]
data = json.load(open(target, encoding='utf-8'))
runtimes = dict(data.get('runtimes') or {})
runtimes.pop('runsc', None)
if runtimes:
    data['runtimes'] = runtimes
else:
    data.pop('runtimes', None)
json.dump(data, open(target, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('剩余 runtimes =', sorted(runtimes))
PY
  systemctl restart docker
else
  echo "  没有 $DAEMON_JSON，跳过"
fi

echo "[3/3] 二进制保留在 /usr/local/bin/runsc（不删）。"
echo "  需要彻底删除时自己决定：rm /usr/local/bin/runsc /usr/local/bin/containerd-shim-runsc-v1"
echo "回滚完成。agent 现在走 LocalRuntime（约束层），不会再声称容器或 gVisor 隔离。"
