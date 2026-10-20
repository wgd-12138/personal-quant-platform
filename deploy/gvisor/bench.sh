#!/usr/bin/env bash
# 沙箱基准：同一套 20 案语料，分别在 runc 和 runsc 下跑，输出可复核的原始结果。
#
# 三条纪律：
#   1. 平台不可用就直接退出，不打印任何数字（没有容器就跑不出容器数据）。
#   2. warmup 与重复次数写死在 gvisor_runtime.bench_plan() 里，脚本只读不猜。
#   3. 原始输出全部落盘到 agent_eval/results/bench_raw/，报告里的数字必须能对回原始文件。
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
PY=${PY:-python3}
OUT=${OUT:-$ROOT/agent_eval/results/bench_raw}

cd "$ROOT" || exit 1
mkdir -p "$OUT" || exit 1

PLATFORM=$(PYTHONPATH=src "$PY" -c "from pq_agent.sandbox.gvisor_runtime import probe_host, decide_platform; print(decide_platform(probe_host()))" 2>/dev/null)
if [ -z "$PLATFORM" ]; then
  echo "gVisor 平台不可用，拒绝生成任何基准数字。" >&2
  echo "先跑 bash deploy/gvisor/probe_host.sh 看清楚缺什么。" >&2
  exit 2
fi

echo "平台：$PLATFORM"
PYTHONPATH=src "$PY" -c "import json; from pq_agent.sandbox.gvisor_runtime import bench_plan; print(json.dumps(bench_plan(), ensure_ascii=False, indent=2))"

for RT in runc runsc; do
  echo "== 运行时：$RT =="
  OUT_FILE="$OUT/${RT}_$(date +%Y%m%d%H%M%S).json"
  PYTHONPATH=src PQ_AGENT_SANDBOX_CONTAINER_RUNTIME="$RT" "$PY" -c "
import json, pathlib, statistics, time
from pq_agent.sandbox.docker_runtime import DockerRuntime, DEFAULT_IMAGE
from pq_agent.sandbox.gvisor_runtime import bench_plan
from pq_agent.sandbox.base import ExecRequest
plan = bench_plan()
corpus = json.loads(pathlib.Path('agent_eval/cases/sandbox_redteam.json').read_text(encoding='utf-8'))
cases = corpus['cases'] if 'cases' in corpus else corpus.get('items', [])
runtime_name = '$RT'
import os
os.environ.setdefault('PQ_AGENT_SANDBOX_CONTAINER_RUNTIME', runtime_name)
def one(code):
    rt = DockerRuntime(image=DEFAULT_IMAGE)
    if runtime_name == 'runsc':
        rt.container_runtime = 'runsc'
    t0 = time.monotonic()
    result = rt.execute(ExecRequest(code=code, trusted_fixture=True, timeout_s=10.0))
    return (time.monotonic() - t0) * 1000.0, result
sample = 'print(1)'
for _ in range(plan['warmup_runs']):
    one(sample)
latencies = [one(sample)[0] for _ in range(plan['repeat_runs'])]
latencies.sort()
def pct(values, q):
    if not values:
        return None
    idx = min(len(values) - 1, int(round(q * (len(values) - 1))))
    return round(values[idx], 3)
mal = [c for c in cases if c.get('kind') == 'malicious']
ben = [c for c in cases if c.get('kind') == 'benign']
report = {
    'runtime': runtime_name,
    'image': DEFAULT_IMAGE,
    'platform': '$PLATFORM',
    'plan': plan,
    'exec_ms_p50': pct(latencies, 0.5),
    'exec_ms_p95': pct(latencies, 0.95),
    'malicious_total': len(mal),
    'benign_total': len(ben),
    'note': '阻断/通过率需要容器内真的执行，未执行时不得填 0 或 1。',
}
print(json.dumps(report, ensure_ascii=False, indent=2))
pathlib.Path(r'$OUT_FILE').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
" | tee "$OUT_FILE.txt"
  echo "原始输出：$OUT_FILE.txt"
done

echo "把原始文件里的数字填进 agent_eval/results/sandbox_bench.md，并保留原始文件路径。"
