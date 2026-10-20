"""pq_agent 命令行入口。

A0 阶段只有 --version；A4-01 增加 APM 栈的 up / status / down / doctor；A5-03 增加离线 demo。

关于 apm 子命令的边界，写在最前面：

1. `apm up` / `apm down` 只**打印**该执行的 docker compose 命令，默认不执行。
   原因不只是「本机没装 Docker」：一个 agent 项目的命令行入口如果能随手起容器，
   它就成了一个没人审计的提权通道。要真执行必须显式加 --run。
2. `apm status` 只读探测，永远不启动任何东西。它回答的是「现在通不通」，
   不是「帮我弄通」。
3. `apm doctor` 是 status 加配置指纹，输出可直接落盘的 JSON 事实卡。
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

from pq_agent import __version__

DEFAULT_FACT_CARD = "agent_eval/results/apm_environment.json"


def _repo_root() -> pathlib.Path:
    """从本文件位置反推工程根：src/pq_agent/__main__.py → 上三级。"""
    return pathlib.Path(__file__).resolve().parents[2]


def compose_argv(*, root: pathlib.Path, action: str) -> list[str]:
    """构造 docker compose 命令。action 只允许 up / down / destroy。"""
    if action not in ("up", "down", "destroy"):
        raise ValueError("未知的 compose 动作：" + str(action))
    base = ["docker", "compose", "-f", str(pathlib.Path(root) / "deploy" / "apm" / "docker-compose.yml")]
    if action == "up":
        return base + ["up", "-d"]
    if action == "down":
        # 不加 -v：停栈不等于删数据。要删必须走 destroy，让人有一步后悔的机会。
        return base + ["down"]
    return base + ["down", "-v"]


def cmd_status(args: argparse.Namespace) -> int:
    """体检并打印。--json 输出机器可读，默认输出人看的表格。"""
    from pq_agent.observability import apm_health, render_health

    root = pathlib.Path(args.root) if args.root else _repo_root()
    report = apm_health(
        root=root,
        evidence_path=(pathlib.Path(args.evidence) if args.evidence else None),
    )
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_health(report))
    if args.strict and report.apm_degraded:
        print("严格模式：APM 处于降级状态，退出码 2。")
        return 2
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """体检 + 配置指纹，落一张事实卡。这才是可被审计的产物。"""
    from pq_agent.observability import apm_fact_card, write_fact_card

    root = pathlib.Path(args.root) if args.root else _repo_root()
    card = apm_fact_card(root=root)
    card_path = pathlib.Path(args.out) if args.out else (root / DEFAULT_FACT_CARD)
    written = write_fact_card(card_path, card, root=root)
    print("事实卡：" + str(written))
    print("结论：" + str(card.get("verdict")))
    print("apm_degraded：" + str(card.get("apm_degraded")).lower())
    fingerprint = card.get("config_fingerprint") or {}
    for key in sorted(fingerprint):
        item = fingerprint[key]
        digest = str(item.get("sha256") or "")[:16]
        print("  " + key + " sha256=" + (digest or "<缺失>") + " bytes=" + str(item.get("bytes")))
    if args.strict and card.get("apm_degraded"):
        return 2
    return 0


def _run_or_print(args: argparse.Namespace, *, action: str, note: str) -> int:
    argv = compose_argv(root=pathlib.Path(args.root) if args.root else _repo_root(), action=action)
    if not args.run:
        print("默认不执行。要真执行请加 --run：")
        print("  " + " ".join(argv))
        print(note)
        return 0
    print("执行：" + " ".join(argv))
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
    if proc.stdout:
        print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)
    return int(proc.returncode)


def cmd_up(args: argparse.Namespace) -> int:
    return _run_or_print(args, action="up", note="up 之后必须再跑 apm status；容器起来不等于 APM 通过。")


def cmd_down(args: argparse.Namespace) -> int:
    return _run_or_print(args, action="down", note="down 只停容器，不删数据卷；要删数据用 apm destroy。")


def cmd_destroy(args: argparse.Namespace) -> int:
    return _run_or_print(args, action="destroy", note="destroy 会删除存储卷，历史 trace 数据不可恢复。")


def cmd_demo(args: argparse.Namespace) -> int:
    """5-8 分钟离线演示（REMAINDER 同构入口）。

    真正的入口是 main() 里对 demo 的提前转发：REMAINDER 传不了以 - 开头的参数。
    两条路径都只调用 runner.main，参数的唯一真源是 pq_agent/demo/runner.py。
    """

    from pq_agent.demo import runner

    rest = [str(item) for item in (getattr(args, "demo_args", None) or [])]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    return int(runner.main(rest))


def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器。"""
    parser = argparse.ArgumentParser(
        prog="python -m pq_agent",
        description="personal_quant Agent 扩展入口（A0 环境自检 / A4 APM 运维）。",
    )
    parser.add_argument("--version", action="version", version="pq_agent " + __version__)
    sub = parser.add_subparsers(dest="command")

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", default="", help="工程根，默认自动推断")
        p.add_argument("--json", action="store_true", help="输出 JSON")
        p.add_argument("--strict", action="store_true", help="降级时退出码 2")

    status = sub.add_parser("status", help="APM 栈只读体检")
    _common(status)
    status.add_argument("--evidence", default="", help="端到端证据 JSON 路径")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="APM 体检 + 配置指纹并落事实卡")
    _common(doctor)
    doctor.add_argument("--out", default="", help="事实卡路径，默认 " + DEFAULT_FACT_CARD)
    doctor.set_defaults(func=cmd_doctor)

    for name, handler, helptext in (
        ("up", cmd_up, "打印（或执行）启动 APM 栈的命令"),
        ("down", cmd_down, "打印（或执行）停止 APM 栈的命令"),
        ("destroy", cmd_destroy, "打印（或执行）停止并删除数据卷的命令"),
    ):
        child = sub.add_parser(name, help=helptext)
        _common(child)
        child.add_argument("--run", action="store_true", help="真的执行；默认只打印")
        child.set_defaults(func=handler)

    # demo 子命令在这里只为 --help 列表存在：main() 会先把 demo 转发给 runner.main（见上面的注释），
    # 因为 REMAINDER 吃不掉以 - 开头的参数。这里保留一个同构入口，便于直接调用 build_parser() 的场景。
    demo = sub.add_parser("demo", help="运行 5-8 分钟离线演示（A5-03）")
    demo.add_argument(
        "demo_args",
        nargs=argparse.REMAINDER,
        help="透传给 demo 的参数：--status / --reset / --step <id> / --json / --quiet / --root",
    )
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    """入口函数：解析参数并分发。无子命令时保持 A0 的骨架提示。"""

    tokens = [str(item) for item in (sys.argv[1:] if argv is None else argv)]
    # demo 子命令在 argparse 之前就转发出去。原因：demo 的参数全是以 - 开头的选项，
    # 而 argparse.REMAINDER 只在第一个 token 不像选项时才开始吃参数——实测
    # `python -m pq_agent demo --quiet` 会报 unrecognized arguments。
    # 直接交给 runner.main，参数的唯一真源仍然只有 runner.py 一份。
    if tokens[:1] == ["demo"]:
        from pq_agent.demo import runner

        return int(runner.main(tokens[1:]))
    args = build_parser().parse_args(tokens)
    handler = getattr(args, "func", None)
    if handler is None:
        print("pq_agent 骨架就绪；可用子命令：status / doctor / up / down / destroy / demo")
        return 0
    return int(handler(args))


if __name__ == "__main__":
    sys.exit(main())
