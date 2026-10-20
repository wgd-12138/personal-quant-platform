#!/usr/bin/env bash
# 在 CentOS 上安装 runsc 并把它注册进 Docker。
#
# 注意：本脚本在 Windows 开发机上**从未运行过**。它按 gVisor 官方文档的步骤写，
# 但「没跑过」这件事必须写在文件里，不能靠读者去猜。
set -euo pipefail

RUNSC_VERSION=${RUNSC_VERSION:-latest}
ARCH=$(uname -m)
case "$ARCH" in
  x86_64) GVISOR_ARCH=x86_64 ;;
  aarch64) GVISOR_ARCH=aarch64 ;;
  *) echo "不支持的架构：$ARCH" >&2; exit 1 ;;
esac

if ! command -v docker >/dev/null 2>&1; then
  echo "没有 docker。先装 Docker 再来装 runsc。" >&2
  exit 1
fi

echo "[1/4] 拉取 runsc 发布包（版本：$RUNSC_VERSION，架构：$GVISOR_ARCH）"
URL="https://storage.googleapis.com/gvisor/releases/release/${RUNSC_VERSION}/${GVISOR_ARCH}"
if [ "$RUNSC_VERSION" = "latest" ]; then
  URL="https://storage.googleapis.com/gvisor/releases/release/latest/${GVISOR_ARCH}"
fi

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT
curl -fsSL -o "$TMPDIR/runsc" "$URL/runsc"
curl -fsSL -o "$TMPDIR/runsc.sha512" "$URL/runsc.sha512"
curl -fsSL -o "$TMPDIR/containerd-shim-runsc-v1" "$URL/containerd-shim-runsc-v1"
curl -fsSL -o "$TMPDIR/containerd-shim-runsc-v1.sha512" "$URL/containerd-shim-runsc-v1.sha512"

echo "[2/4] 校验 sha512"
( cd "$TMPDIR" && sha512sum -c runsc.sha512 && sha512sum -c containerd-shim-runsc-v1.sha512 )

echo "[3/4] 安装二进制到 /usr/local/bin"
install -m 0755 "$TMPDIR/runsc" /usr/local/bin/runsc
install -m 0755 "$TMPDIR/containerd-shim-runsc-v1" /usr/local/bin/containerd-shim-runsc-v1

echo "[4/4] 写入 /etc/docker/daemon.json 的 runtimes 段"
bash "$(dirname "$0")/register_runtime.sh"

echo "完成。接着执行：sudo systemctl restart docker && bash deploy/gvisor/verify_gvisor.sh"
