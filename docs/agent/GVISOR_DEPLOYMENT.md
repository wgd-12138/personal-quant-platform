# gVisor 部署合同（GvisorRuntime）

第三层沙箱：把容器运行时从 runc 换成 gVisor 的 runsc，让系统调用先经过一层用户态内核。
本文件记录的是**合同**（这一层承诺什么、不承诺什么）和**部署路径**，不是运行记录。

## 0. 一句话结论

同一套 `SandboxRuntime` 协议现在有**三份实现**，靠接口设计换层，而不是靠改代码换层：

| 实现 | isolation_level | 边界 | 本机状态 |
|---|---|---|---|
| LocalRuntime | constraint_only | 无真隔离 | 可用（约束层） |
| DockerRuntime | container | 容器命名空间 + cgroup | 不可用（无 Docker） |
| GvisorRuntime | gvisor | 容器 + gVisor 用户态内核 | 不可用（非 Linux） |

## 1. 可用性判据（fail-closed）

`decide_platform(facts)` 只在下列全部成立时才给出平台，否则返回空串：

| 顺序 | 条件 | 不成立的后果 |
|---|---|---|
| 1 | 宿主是 Linux | 返回空串：Windows 上 gVisor 不可能运行 |
| 2 | Docker 服务端可达 | 返回空串：没有容器运行时，谈不上换运行时 |
| 3 | Docker 已注册 `runsc` | 返回空串：不注册就等于没有这一层 |
| 4 | `/dev/kvm` 存在且可读 | 存在不可读 → 退回 `ptrace`；不存在 → 退回 `ptrace` |

第 4 条**不是**失败条件：`ptrace` 平台不需要 KVM，隔离仍然成立，只是慢。
但「存在却不可读」这种权限问题必须写进报告，不能悄悄吞掉——`platform_note()` 负责这件事。

探测字段（`HostFacts`）：宿主系统、内核版本、发行版与版本、CentOS EOL、Docker 探针、
`runsc` 路径与版本、已注册 runtime 列表、`/dev/kvm` 是否存在/可读、rootless、Docker 安全选项、探测时间。
**没有一条来自推测**：每一条都是当场跑命令量出来的，量不到就是空值。

## 2. 加固参数：和 DockerRuntime 完全同一份

`GvisorRuntime` 继承 `DockerRuntime`，不复制参数。唯一的区别是 argv 里多一段：

```
docker run --rm -i --runtime runsc --network none --read-only ...
```

为什么必须显式写 `--runtime runsc`：
漏掉它时 Docker 会**静默退回默认的 runc**，容器照样跑起来，日志照样正常，
但这次执行已经不在 gVisor 上了。这类「静默降级」比直接报错危险得多。
所以审计函数多了 `required_runtime` 参数，产出 `runtime_flag_correct` 这一条断言。

## 3. 能力矩阵：gVisor 的 16 条

在 Docker 的 12 条之外多 3 条支持：

| 能力 | 说明 | 由什么强制 |
|---|---|---|
| user_space_kernel | 系统调用由 gVisor 在用户态实现 | runsc Sentry |
| syscall_filtering | 未实现的系统调用按平台策略拒绝或降级 | runsc 平台层 |
| reduced_host_kernel_surface | 宿主内核只处理被转发的调用 | runsc + `--runtime runsc` |

并且**明确留一条不支持**：

| 能力 | 状态 | 理由 |
|---|---|---|
| kernel_side_channel_proof | unsupported | 仍共享宿主内核，侧信道与部分系统调用语义差异无法在本层证明不存在 |

留这条不是为了好看。能力矩阵的作用就是让上层知道「哪件事我没做到」；
一张全是 supported 的表等于没有信息量。

## 4. 云端暴露面

| 规则 | 值 |
|---|---|
| 绑定地址 | `127.0.0.1` |
| 允许公网暴露 | 否 |
| 必须走 SSH 隧道 | 是 |

笔记本访问方式：

```
ssh -L 8123:127.0.0.1:8123 <user>@<cloud-host>
```

Runner、OTel Collector（4317/4318）、SkyWalking（11800/12800）一律不绑公网地址。
验收脚本会检查 `ss -ltnp` 里有没有这些端口出现在 `0.0.0.0` 或 `[::]` 上。

## 5. 部署路径

```
probe_host.sh  ->  install_runsc.sh  ->  register_runtime.sh  ->  verify_gvisor.sh
                                                                          |
                                                     uninstall_runsc.sh（回滚）
```

`register_runtime.sh` 只往 `daemon.json` 加 `runtimes`，**不动 `default-runtime`**：
把 runsc 设成默认会影响机器上所有容器（构建、监控、其他服务），
而我们的决定只是「agent 代码执行这一路走 runsc」。

回滚顺序是刻意的：先关 agent 侧开关，再摘 Docker runtime，
否则会出现「agent 以为在用 runsc、Docker 上已经没有 runsc」的窗口。

## 6. 证据边界（最重要的一节）

| 说法 | 有没有证据 |
|---|---|
| argv 里确实带 `--runtime runsc` 与全部加固参数 | 有（单测 + 审计函数实测） |
| 平台判定与 fail-closed 逻辑正确 | 有（单测覆盖 5 种事实组合） |
| 本机 Windows 的 `gvisor_report()` 报 unavailable 且原因正确 | 有（实测） |
| **容器隔离在真实环境成立** | **无**：本机没有 Docker |
| **gVisor 隔离在真实环境成立** | **无**：本机不是 Linux，云端也没有可用凭据 |
| 镜像 digest 已被核对 | **无**：`docker.runtime_config.json` 里标 `unverified_on_this_host` |
| runc vs runsc 的性能数字 | **无**：`sandbox_bench.md` 目前是空模板 |

前三条能讲，后四条不能讲。把「配置正确」说成「隔离已验证」是这一层最容易被戳穿的谎。

## 7. 学习产出

1. **换一层隔离，接口不该换。** 三份 runtime 实现同一个 Protocol，
   图、工具层、API 层完全不知道底下是子进程还是 gVisor。
2. **显式参数比默认值安全。** `--runtime runsc` 必须写出来，
   因为默认值会静默生效，而静默生效的默认值是审计的盲区。
3. **能力矩阵要留 unsupported。** 全 supported 的表说明作者没认真想边界。
4. **探测要用「能不能问出结果」当判据。** `docker` 命令存在 ≠ 守护进程能用；
   同理，`runsc` 装了 ≠ 已注册进 Docker。
5. **没跑过就写没跑过。** 部署脚本可以写，但文件头必须写明从未执行。

## 8. 回滚

- 代码层：删除 `src/pq_agent/sandbox/gvisor_runtime.py`、`deploy/gvisor/`、
  `tests/agent/sandbox/test_gvisor.py`、`docs/agent/GVISOR_DEPLOYMENT.md`，
  还原 `src/pq_agent/sandbox/__init__.py`、`capabilities.py`、`base.py`、`docker_runtime.py` 的改动。
- 部署层：`bash deploy/gvisor/uninstall_runsc.sh`（摘 runtime、重启 Docker、保留二进制与用户数据）。
- 配置层：`PQ_AGENT_SANDBOX_RUNTIME` 置为 `local` 或删除。
