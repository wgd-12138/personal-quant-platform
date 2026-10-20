# 沙箱基准：runc vs runsc

> **本文件目前只是模板，没有任何实测数字。**
> 原因：本机（Windows）没有 Docker、没有 Linux 内核，容器与 gVisor 都跑不起来。
> 按项目规矩，「不可测就写不可测」，绝不用估算值或从别处抄来的数字填空。
> 数字只允许由 `deploy/gvisor/bench.sh` 在云端 CentOS 上跑出来后填入。

## 1. 环境（必须逐项如实填写，缺一项这次测量就作废）

| 项 | 值 |
|---|---|
| 采集日期 | TODO |
| 云厂商 / 机型 | TODO |
| 发行版 | TODO（来自 /etc/os-release 的 PRETTY_NAME） |
| 内核版本 | TODO（uname -r） |
| Docker 版本 | TODO |
| runsc 版本 | TODO（runsc --version） |
| gVisor 平台 | TODO（kvm 或 ptrace，来自 probe_host） |
| /dev/kvm 可读 | TODO（是 / 否） |
| 镜像 | python:3.12.7-slim@sha256:...（必须写 digest） |
| 宿主 CPU / 内存 | TODO |

## 2. 配置（本次测量使用的固定口径）

| 项 | 值 |
|---|---|
| warmup 次数 | 3 |
| 每项重复次数 | 30 |
| 样例数（红队语料） | 20（10 恶意 + 10 良性） |
| 内存上限 | 256m |
| pids 上限 | 64 |
| CPU 配额 | 1.0 |
| tmpfs | /work:rw,noexec,nosuid,size=16m |
| 用户 | 65532:65532 |
| 网络 | none |

## 3. 结果

| 指标 | runc | runsc | 说明 |
|---|---|---|---|
| startup_ms p50 | TODO | TODO | 容器从 run 到进程启动 |
| startup_ms p95 | TODO | TODO | |
| exec_ms p50 | TODO | TODO | 固定样例的执行耗时 |
| exec_ms p95 | TODO | TODO | |
| 恶意样本阻断率 | TODO | TODO | 10 条恶意样本中被拦下的比例 |
| 良性样本通过率 | TODO | TODO | 10 条良性样本中正常跑完的比例 |
| 误杀率 | TODO | TODO | 良性样本被拦下的比例 |
| 超时率 | TODO | TODO | |
| 残留进程数 | TODO | TODO | 测量结束后宿主上残留的容器/进程数 |

**恶意阻断率与良性通过率必须分开报。** 只报一个「安全率」等于把误杀算成安全。

## 4. 统计方法

- 每项指标先跑 3 次 warmup 丢弃，再连续跑 30 次取 p50 / p95。
- 阻断率按「条」统计，分母是语料条数，不是运行次数。
- 所有数字必须来自同一次会话、同一台机器、同一镜像 digest；跨机器不能合并。
- 记录原始输出文件名与路径，便于复核。

## 5. 不确定性（必须写，不能省）

- TODO：本次测量没有覆盖到的东西（例如并发、长时间运行、内核版本差异）。
- TODO：p50 / p95 的样本量是否足以支撑结论（30 次只能给方向，不能给精确分布）。
- TODO：什么条件下这些数字会失效。

## 6. 结论边界

- Windows 上的结果**不得**外推到这里，反之亦然。
- 没有 Docker / runsc 的环境里，本文件必须保持「模板」状态。
- 哪怕数字齐全，也只能说「在这台机器、这个镜像、这套参数下如此」，不能说「runsc 一定更慢」。
