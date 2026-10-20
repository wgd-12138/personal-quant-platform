# deploy/gvisor：把 gVisor（runsc）接进 Docker

这个目录里的东西全部是给**云端 Linux 机器**（腾讯云 CentOS）用的。
本机是 Windows，gVisor 需要 Linux 内核，所以这里没有任何一行在本机跑过——
文件本身按「可以在 CentOS 上按顺序执行」写，但**没有实测证据**，不要当成已验证的部署。

## 为什么要这一层

| 层 | 边界 | 能挡住什么 | 挡不住什么 |
|---|---|---|---|
| LocalRuntime | 无（只是约束） | 误写的危险调用 | 几乎所有东西 |
| DockerRuntime（runc） | 容器命名空间 + cgroup | 网络、文件、权限、资源 | 内核漏洞、容器逃逸 |
| GVisorRuntime（runsc） | 容器 + gVisor 用户态内核 | 上面全部 + 大部分系统调用不直达宿主内核 | 共享宿主内核带来的侧信道 |

三层的接口是同一个 `SandboxRuntime` 协议，换层只换 `PQ_AGENT_SANDBOX_RUNTIME` 的值。

## 执行顺序（在云端 CentOS 上）

```bash
# 1. 探测。任何一步失败都不要继续。
bash deploy/gvisor/probe_host.sh > /tmp/pq_gvisor_probe.json

# 2. 安装 runsc（会写入 /usr/local/bin，并写 /etc/docker/daemon.json）
sudo bash deploy/gvisor/install_runsc.sh

# 3. 重启 Docker 并确认 runtime 已注册
sudo bash deploy/gvisor/register_runtime.sh

# 4. 验收：加固参数、runtime 注册、红队语料、暴露面
bash deploy/gvisor/verify_gvisor.sh

# 5. 回滚（停用 gVisor，回到 runc；不删用户数据）
sudo bash deploy/gvisor/uninstall_runsc.sh
```

## 硬规矩

1. `--runtime=runsc` 必须显式出现在 argv 里。少了它 Docker 会静默退回 runc，
   容器照跑，隔离级别却降了一档——所以审计里有 `runtime_flag_correct` 这一条。
2. runsc 不可用时**拒绝执行**，不回落到 runc，也不回落到 LocalRuntime。
   回落会让「这次跑在 gVisor 上」这句话变成假的。
3. 镜像必须用 digest 固定。标签会被上游重新指向，digest 不会。
4. 云端服务（agent API、Runner、Collector、SkyWalking 面板）只监听 127.0.0.1，
   笔记本走 SSH 隧道。公网端口一律不开。
5. `daemon.json` 只加 runtime，不动 default-runtime：
   把 runsc 设成默认会让所有容器（包括构建、监控）一起换内核，那不是我们要的那个决定。

## 已知未完成的事

- 本机没有 Docker、没有 Linux 内核，**容器隔离与 gVisor 隔离都尚未在真实环境验证**。
- 镜像 digest 目前标 `unverified_on_this_host`：必须在装好 Docker 的机器上核对后才有资格说「供应链已锁定」。
- `agent_eval/results/sandbox_bench.md` 目前只是模板，没有填任何数字。
