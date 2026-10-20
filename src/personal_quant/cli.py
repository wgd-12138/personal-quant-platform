from __future__ import annotations

import argparse
import datetime as dt
import time
from pathlib import Path

from personal_quant.data_center.positions_importer import import_latest_positions_export, import_positions_csv
from personal_quant.data_center.positions import load_positions
from personal_quant.data_center.providers import fetch_market_dataset
from personal_quant.data_center.readiness import check_inputs, render_readiness_report
from personal_quant.market_analysis.classifier import summarize_market
from personal_quant.notifier.activation import write_push_activation_report
from personal_quant.notifier.channel_test import send_push_channel_test
from personal_quant.notifier.go_live import write_push_go_live_report
from personal_quant.notifier.market_monitor import (
    MarketMonitorConfig,
    run_market_monitor_loop,
    run_market_monitor_once,
)
from personal_quant.notifier.position_monitor import (
    PositionMonitorConfig,
    run_position_monitor_loop,
    run_position_monitor_once,
)
from personal_quant.notifier.push import send_push_slot
from personal_quant.notifier.schedule import PUSH_SLOTS, write_push_schedule_report, write_windows_task_script
from personal_quant.notifier.status import write_push_status_report
from personal_quant.notifier.windows_tasks import write_windows_task_status_report
from personal_quant.report_builder.archive import report_archive_dir
from personal_quant.report_builder.audit import audit_daily_report
from personal_quant.report_builder.daily_pack import build_daily_pack_report, build_daily_report_sections
from personal_quant.report_builder.manifest import write_manifest_report
from personal_quant.report_builder.run_audit import (
    add_expected_dates,
    load_expected_dates,
    select_next_expected_date,
    write_failed_run_audit_report,
    write_run_audit_report,
    write_run_summary_report,
    write_stage_gate_report,
    write_trial_status_report,
)
from personal_quant.report_builder.summary import build_summary_report


def main() -> None:
    parser = argparse.ArgumentParser(description="个人量化分析与交易平台")
    parser.add_argument("--version", action="store_true", help="显示版本信息")
    subparsers = parser.add_subparsers(dest="command")
    summary_parser = subparsers.add_parser("summary", help="生成本地盘面信息总结")
    summary_parser.add_argument("--as-of", default=dt.date.today().isoformat(), help="截止日期，格式 YYYY-MM-DD")
    summary_parser.add_argument("--output-dir", default="reports", help="报告归档根目录")
    daily_pack_parser = subparsers.add_parser("daily-pack", help="生成每日完整推送内容")
    daily_pack_parser.add_argument("--as-of", default=dt.date.today().isoformat(), help="截止日期，格式 YYYY-MM-DD")
    daily_pack_parser.add_argument("--output-dir", default="reports", help="报告归档根目录")
    daily_pack_parser.add_argument("--positions", default=None, help="真实持仓 CSV 路径；不提供则不生成逐股持仓建议")
    daily_pack_parser.add_argument("--split", action="store_true", help="同时输出汇总报告、推送预览、清单和各类分报告")
    daily_pack_parser.add_argument("--require-positions", action="store_true", help="严格要求真实持仓；缺失时命令失败")
    daily_pack_parser.add_argument("--ignore-positions", action="store_true", help="本阶段暂不关注持仓个股，不把缺持仓作为缺口")
    check_parser = subparsers.add_parser("check-inputs", help="检查完整推送所需输入是否就绪")
    check_parser.add_argument("--as-of", default=dt.date.today().isoformat(), help="截止日期，格式 YYYY-MM-DD")
    check_parser.add_argument("--positions", default="data/positions.csv", help="真实持仓 CSV 路径")
    check_parser.add_argument("--output-dir", default="reports", help="报告归档根目录")
    check_parser.add_argument("--ignore-positions", action="store_true", help="本阶段暂不关注持仓个股，不把缺持仓作为缺口")
    import_parser = subparsers.add_parser("positions-import", help="导入东方财富导出的持仓 CSV")
    import_parser.add_argument("--source", required=True, help="东方财富导出的持仓 CSV 路径")
    import_parser.add_argument("--output", default="data/positions.csv", help="标准持仓 CSV 输出路径")
    import_parser.add_argument("--encoding", default="utf-8-sig", help="源文件编码，常见为 utf-8-sig 或 gbk")
    watch_parser = subparsers.add_parser("positions-watch", help="监听导出目录并同步最新持仓 CSV")
    watch_parser.add_argument("--source-dir", default="data/imports", help="东方财富持仓导出目录")
    watch_parser.add_argument("--pattern", default="*.csv", help="导出文件匹配规则")
    watch_parser.add_argument("--output", default="data/positions.csv", help="标准持仓 CSV 输出路径")
    watch_parser.add_argument("--encoding", default="utf-8-sig", help="源文件编码，常见为 utf-8-sig 或 gbk")
    watch_parser.add_argument("--interval", type=float, default=10.0, help="监听间隔秒数")
    watch_parser.add_argument("--once", action="store_true", help="只同步一次最新文件后退出")
    run_audit_parser = subparsers.add_parser("run-audit", help="记录一次本地日报试运行结果")
    run_audit_parser.add_argument("--as-of", default=dt.date.today().isoformat(), help="截止日期，格式 YYYY-MM-DD")
    run_audit_parser.add_argument("--reports-dir", default="reports", help="已生成日报目录")
    run_audit_parser.add_argument("--output-dir", default="planning/runs", help="运行记录输出目录")
    run_audit_parser.add_argument("--source-command", default=None, help="本次日报生成命令，写入运行记录")
    run_summary_parser = subparsers.add_parser("run-summary", help="汇总本地试运行进度")
    run_summary_parser.add_argument("--runs-dir", default="planning/runs", help="运行记录目录")
    run_summary_parser.add_argument("--target-days", type=int, default=10, help="目标连续通过交易日数量")
    run_summary_parser.add_argument("--expected-dates", default=None, help="期望交易日清单，每行一个 YYYY-MM-DD")
    trial_run_parser = subparsers.add_parser("trial-run", help="一键执行本地试运行：日报、审计、汇总")
    trial_run_parser.add_argument("--as-of", default=dt.date.today().isoformat(), help="截止日期，格式 YYYY-MM-DD")
    trial_run_parser.add_argument("--reports-dir", default="reports", help="报告归档根目录")
    trial_run_parser.add_argument("--runs-dir", default="planning/runs", help="运行记录目录")
    trial_run_parser.add_argument("--positions", default=None, help="真实持仓 CSV 路径；不提供则不生成逐股持仓建议")
    trial_run_parser.add_argument("--require-positions", action="store_true", help="严格要求真实持仓；缺失时命令失败")
    trial_run_parser.add_argument("--ignore-positions", action="store_true", help="本阶段暂不关注持仓个股，不把缺持仓作为缺口")
    trial_run_parser.add_argument("--target-days", type=int, default=10, help="目标连续通过交易日数量")
    trial_run_parser.add_argument("--expected-dates", default=None, help="期望交易日清单，每行一个 YYYY-MM-DD")
    trial_run_parser.add_argument("--allow-rerun", action="store_true", help="允许覆盖已有运行记录，仅用于明确重跑")
    trial_next_parser = subparsers.add_parser("trial-run-next", help="运行期望交易日清单中的下一个未运行日期")
    trial_next_parser.add_argument("--reports-dir", default="reports", help="报告归档根目录")
    trial_next_parser.add_argument("--runs-dir", default="planning/runs", help="运行记录目录")
    trial_next_parser.add_argument("--positions", default=None, help="真实持仓 CSV 路径；不提供则不生成逐股持仓建议")
    trial_next_parser.add_argument("--require-positions", action="store_true", help="严格要求真实持仓；缺失时命令失败")
    trial_next_parser.add_argument("--ignore-positions", action="store_true", help="本阶段暂不关注持仓个股，不把缺持仓作为缺口")
    trial_next_parser.add_argument("--target-days", type=int, default=10, help="目标连续通过交易日数量")
    trial_next_parser.add_argument("--expected-dates", default="planning/runs/expected_dates.txt", help="期望交易日清单，每行一个 YYYY-MM-DD")
    trial_gate_parser = subparsers.add_parser("trial-gate", help="检查本地试运行是否允许进入推送阶段")
    trial_gate_parser.add_argument("--runs-dir", default="planning/runs", help="运行记录目录")
    trial_status_parser = subparsers.add_parser("trial-status", help="查看本地试运行当前状态和下一步")
    trial_status_parser.add_argument("--reports-dir", default="reports", help="报告输出目录")
    trial_status_parser.add_argument("--runs-dir", default="planning/runs", help="运行记录目录")
    trial_status_parser.add_argument("--target-days", type=int, default=10, help="目标连续通过交易日数量")
    trial_status_parser.add_argument("--expected-dates", default="planning/runs/expected_dates.txt", help="期望交易日清单，每行一个 YYYY-MM-DD")
    trial_status_parser.add_argument("--ignore-positions", action="store_true", help="本阶段暂不关注持仓个股，不把缺持仓作为缺口")
    expected_dates_parser = subparsers.add_parser("expected-dates-add", help="添加已确认的试运行交易日")
    expected_dates_parser.add_argument("--path", default="planning/runs/expected_dates.txt", help="期望交易日清单路径")
    expected_dates_parser.add_argument("--date", action="append", required=True, help="要添加的交易日，格式 YYYY-MM-DD；可重复传入")
    push_schedule_parser = subparsers.add_parser("push-schedule", help="生成每日生成和推送时间表")
    push_schedule_parser.add_argument("--output", default="planning/runs/push_schedule.md", help="时间表输出路径")
    push_schedule_parser.add_argument("--reports-dir", default="reports", help="报告归档根目录")
    push_schedule_parser.add_argument("--runs-dir", default="planning/runs", help="运行记录目录")
    push_schedule_parser.add_argument("--target-days", type=int, default=10, help="目标连续通过交易日数量")
    push_schedule_parser.add_argument("--expected-dates", default="planning/runs/expected_dates.txt", help="期望交易日清单")
    push_schedule_parser.add_argument("--positions", default=None, help="真实持仓 CSV 路径；用于生成带持仓入口的推送命令")
    push_schedule_parser.add_argument("--ignore-positions", action="store_true", help="本阶段暂不关注持仓个股")
    push_send_parser = subparsers.add_parser("push-send", help="按时间节点发送已生成的推送预览")
    push_send_parser.add_argument("--as-of", required=True, help="报告日期，格式 YYYY-MM-DD")
    push_send_parser.add_argument("--slot", required=True, choices=[slot.key for slot in PUSH_SLOTS], help="推送时间节点")
    push_send_parser.add_argument("--reports-dir", default="reports", help="报告归档根目录")
    push_send_parser.add_argument("--output-dir", default="planning/runs", help="推送回执输出目录")
    push_send_parser.add_argument("--webhook-url", default=None, help="飞书自定义机器人 webhook；不填则读取 FEISHU_WEBHOOK_URL")
    push_send_parser.add_argument("--live", action="store_true", help="真实发送；不加时只做 dry-run")
    push_run_parser = subparsers.add_parser("push-run", help="按时间节点生成日报并推送")
    push_run_parser.add_argument("--as-of", required=True, help="报告日期，格式 YYYY-MM-DD")
    push_run_parser.add_argument("--slot", required=True, choices=[slot.key for slot in PUSH_SLOTS], help="推送时间节点")
    push_run_parser.add_argument("--reports-dir", default="reports", help="报告归档根目录")
    push_run_parser.add_argument("--output-dir", default="planning/runs", help="推送回执输出目录")
    push_run_parser.add_argument("--positions", default=None, help="真实持仓 CSV 路径；不提供则不生成逐股持仓建议")
    push_run_parser.add_argument("--require-positions", action="store_true", help="严格要求真实持仓；缺失时命令失败")
    push_run_parser.add_argument("--ignore-positions", action="store_true", help="本阶段暂不关注持仓个股")
    push_run_parser.add_argument("--webhook-url", default=None, help="飞书自定义机器人 webhook；不填则读取 FEISHU_WEBHOOK_URL")
    push_run_parser.add_argument("--live", action="store_true", help="真实发送；不加时只做 dry-run")
    push_daily_parser = subparsers.add_parser("push-daily", help="按时间节点自动取日期、生成日报并推送")
    push_daily_parser.add_argument("--slot", required=True, choices=[slot.key for slot in PUSH_SLOTS], help="推送时间节点")
    push_daily_parser.add_argument("--reports-dir", default="reports", help="报告归档根目录")
    push_daily_parser.add_argument("--output-dir", default="planning/runs", help="推送回执输出目录")
    push_daily_parser.add_argument("--expected-dates", default="planning/runs/expected_dates.txt", help="期望交易日清单")
    push_daily_parser.add_argument("--positions", default=None, help="真实持仓 CSV 路径；不提供则不生成逐股持仓建议")
    push_daily_parser.add_argument("--require-positions", action="store_true", help="严格要求真实持仓；缺失时命令失败")
    push_daily_parser.add_argument("--ignore-positions", action="store_true", help="本阶段暂不关注持仓个股")
    push_daily_parser.add_argument("--webhook-url", default=None, help="飞书自定义机器人 webhook；不填则读取 FEISHU_WEBHOOK_URL")
    push_daily_parser.add_argument("--live", action="store_true", help="真实发送；不加时只做 dry-run")
    windows_tasks_parser = subparsers.add_parser("push-windows-tasks", help="生成 Windows 定时任务注册脚本")
    windows_tasks_parser.add_argument("--output", default="planning/runs/register_push_tasks.ps1", help="脚本输出路径")
    windows_tasks_parser.add_argument("--project-dir", default=None, help="项目目录；不填则使用当前目录")
    windows_tasks_parser.add_argument("--reports-dir", default="reports", help="报告归档根目录")
    windows_tasks_parser.add_argument("--runs-dir", default="planning/runs", help="运行记录目录")
    windows_tasks_parser.add_argument("--expected-dates", default="planning/runs/expected_dates.txt", help="期望交易日清单")
    windows_tasks_parser.add_argument("--positions", default=None, help="真实持仓 CSV 路径；用于计划任务生成逐股持仓内容")
    windows_tasks_parser.add_argument("--python", default="python", help="计划任务使用的 Python 命令")
    windows_tasks_parser.add_argument("--ignore-positions", action="store_true", help="本阶段暂不关注持仓个股")
    windows_tasks_parser.add_argument("--live", action="store_true", help="生成真实发送任务；不加时生成 dry-run 任务")
    monitor_once_parser = subparsers.add_parser("position-monitor-once", help="单次检查持仓触发条件并按需提醒")
    _add_position_monitor_args(monitor_once_parser)
    monitor_parser = subparsers.add_parser("position-monitor", help="盘中持续监控持仓触发条件")
    _add_position_monitor_args(monitor_parser)
    monitor_parser.add_argument("--interval-seconds", type=int, default=60, help="轮询间隔秒数")
    monitor_parser.add_argument("--market-start", default="09:30", help="监控开始时间，格式 HH:MM")
    monitor_parser.add_argument("--market-end", default="15:00", help="监控结束时间，格式 HH:MM")
    market_once_parser = subparsers.add_parser("market-monitor-once", help="单次检查盘中突发利空、资金出逃和利好建仓观察")
    _add_market_monitor_args(market_once_parser)
    market_parser = subparsers.add_parser("market-monitor", help="盘中持续监控市场突发事件")
    _add_market_monitor_args(market_parser)
    market_parser.add_argument("--interval-seconds", type=int, default=60, help="轮询间隔秒数")
    market_parser.add_argument("--market-start", default="09:30", help="监控开始时间，格式 HH:MM")
    market_parser.add_argument("--market-end", default="15:00", help="监控结束时间，格式 HH:MM")
    push_status_parser = subparsers.add_parser("push-status", help="检查真实推送上线条件")
    push_status_parser.add_argument("--output", default="planning/runs/push_status.md", help="自检报告输出路径")
    push_status_parser.add_argument("--reports-dir", default="reports", help="报告归档根目录")
    push_status_parser.add_argument("--runs-dir", default="planning/runs", help="运行记录目录")
    push_status_parser.add_argument("--expected-dates", default="planning/runs/expected_dates.txt", help="期望交易日清单")
    push_status_parser.add_argument("--positions", default="data/positions.csv", help="真实持仓 CSV 路径")
    push_status_parser.add_argument("--webhook-url", default=None, help="仅用于本次自检；不填则检查 FEISHU_WEBHOOK_URL")
    push_test_parser = subparsers.add_parser("push-test", help="发送飞书推送通道测试消息")
    push_test_parser.add_argument("--output-dir", default="planning/runs", help="通道测试回执输出目录")
    push_test_parser.add_argument("--webhook-url", default=None, help="飞书自定义机器人 webhook；不填则读取 FEISHU_WEBHOOK_URL")
    push_test_parser.add_argument("--live", action="store_true", help="真实发送测试消息；不加时只做 dry-run")
    task_status_parser = subparsers.add_parser("push-windows-task-status", help="检查 Windows 定时任务是否已注册")
    task_status_parser.add_argument("--output", default="planning/runs/push_windows_task_status.md", help="定时任务状态输出路径")
    go_live_parser = subparsers.add_parser("push-go-live", help="生成推送上线门禁报告")
    go_live_parser.add_argument("--output", default="planning/runs/push_go_live.md", help="上线门禁输出路径")
    go_live_parser.add_argument("--runs-dir", default="planning/runs", help="运行记录目录")
    go_live_parser.add_argument("--expected-dates", default="planning/runs/expected_dates.txt", help="期望交易日清单")
    activation_parser = subparsers.add_parser("push-activation", help="生成推送激活清单")
    activation_parser.add_argument("--output", default="planning/runs/push_activation.md", help="激活清单输出路径")
    activation_parser.add_argument("--runs-dir", default="planning/runs", help="运行记录目录")
    activation_parser.add_argument("--reports-dir", default="reports", help="报告归档根目录")
    activation_parser.add_argument("--expected-dates", default="planning/runs/expected_dates.txt", help="期望交易日清单")
    activation_parser.add_argument("--project-dir", default=None, help="项目目录；不填则使用当前目录")
    args = parser.parse_args()
    if args.version:
        print("personal-quant-platform 0.1.0")
    elif args.command == "summary":
        report_path = generate_summary(dt.date.fromisoformat(args.as_of), Path(args.output_dir))
        print(report_path)
    elif args.command == "daily-pack":
        positions_path = Path(args.positions) if args.positions else None
        report_path = generate_daily_pack(
            dt.date.fromisoformat(args.as_of),
            Path(args.output_dir),
            positions_path,
            split=args.split,
            require_positions=args.require_positions,
            focus_positions=not args.ignore_positions,
        )
        print(report_path)
    elif args.command == "check-inputs":
        report_path = generate_readiness_report(
            dt.date.fromisoformat(args.as_of),
            Path(args.output_dir),
            Path(args.positions),
            focus_positions=not args.ignore_positions,
        )
        print(report_path)
    elif args.command == "positions-import":
        output_path = import_positions_file(Path(args.source), Path(args.output), args.encoding)
        print(output_path)
    elif args.command == "positions-watch":
        source_path = watch_positions_exports(
            Path(args.source_dir),
            Path(args.output),
            pattern=args.pattern,
            encoding=args.encoding,
            interval=args.interval,
            once=args.once,
        )
        print(source_path)
    elif args.command == "run-audit":
        report_path = generate_run_audit(
            dt.date.fromisoformat(args.as_of),
            Path(args.reports_dir),
            Path(args.output_dir),
            command=args.source_command,
        )
        print(report_path)
    elif args.command == "run-summary":
        expected_dates_path = Path(args.expected_dates) if args.expected_dates else None
        report_path = generate_run_summary(
            Path(args.runs_dir),
            target_days=args.target_days,
            expected_dates_path=expected_dates_path,
        )
        print(report_path)
    elif args.command == "trial-run":
        positions_path = Path(args.positions) if args.positions else None
        report_path = generate_trial_run(
            dt.date.fromisoformat(args.as_of),
            Path(args.reports_dir),
            Path(args.runs_dir),
            positions_path=positions_path,
            require_positions=args.require_positions,
            focus_positions=not args.ignore_positions,
            target_days=args.target_days,
            expected_dates_path=Path(args.expected_dates) if args.expected_dates else None,
            allow_rerun=args.allow_rerun,
        )
        print(report_path)
    elif args.command == "trial-run-next":
        positions_path = Path(args.positions) if args.positions else None
        report_path = generate_trial_run_next(
            Path(args.reports_dir),
            Path(args.runs_dir),
            Path(args.expected_dates),
            positions_path=positions_path,
            require_positions=args.require_positions,
            focus_positions=not args.ignore_positions,
            target_days=args.target_days,
        )
        print(report_path)
    elif args.command == "trial-gate":
        report_path = generate_trial_gate(Path(args.runs_dir))
        print(report_path)
    elif args.command == "trial-status":
        report_path = generate_trial_status(
            Path(args.runs_dir),
            Path(args.expected_dates),
            reports_dir=Path(args.reports_dir),
            target_days=args.target_days,
            focus_positions=not args.ignore_positions,
        )
        print(report_path)
    elif args.command == "expected-dates-add":
        output_path = add_expected_trading_dates(Path(args.path), args.date)
        print(output_path)
    elif args.command == "push-schedule":
        output_path = generate_push_schedule(
            Path(args.output),
            reports_dir=Path(args.reports_dir),
            runs_dir=Path(args.runs_dir),
            target_days=args.target_days,
            expected_dates_path=Path(args.expected_dates),
            positions_path=Path(args.positions) if args.positions else None,
            focus_positions=not args.ignore_positions,
        )
        print(output_path)
    elif args.command == "push-send":
        output_path = generate_push_send(
            dt.date.fromisoformat(args.as_of),
            Path(args.reports_dir),
            args.slot,
            Path(args.output_dir),
            live=args.live,
            webhook_url=args.webhook_url,
        )
        print(output_path)
    elif args.command == "push-run":
        positions_path = Path(args.positions) if args.positions else None
        output_path = generate_push_run(
            dt.date.fromisoformat(args.as_of),
            args.slot,
            Path(args.reports_dir),
            Path(args.output_dir),
            positions_path=positions_path,
            require_positions=args.require_positions,
            focus_positions=not args.ignore_positions,
            live=args.live,
            webhook_url=args.webhook_url,
        )
        print(output_path)
    elif args.command == "push-daily":
        positions_path = Path(args.positions) if args.positions else None
        output_path = generate_push_daily(
            args.slot,
            Path(args.reports_dir),
            Path(args.output_dir),
            Path(args.expected_dates),
            positions_path=positions_path,
            require_positions=args.require_positions,
            focus_positions=not args.ignore_positions,
            live=args.live,
            webhook_url=args.webhook_url,
        )
        print(output_path)
    elif args.command == "push-windows-tasks":
        output_path = generate_windows_task_script(
            Path(args.output),
            project_dir=Path(args.project_dir) if args.project_dir else Path.cwd(),
            reports_dir=Path(args.reports_dir),
            runs_dir=Path(args.runs_dir),
            expected_dates_path=Path(args.expected_dates),
            positions_path=Path(args.positions) if args.positions else None,
            python_command=args.python,
            focus_positions=not args.ignore_positions,
            live=args.live,
        )
        print(output_path)
    elif args.command == "position-monitor-once":
        output_path = generate_position_monitor_once(
            positions_path=Path(args.positions),
            output_dir=Path(args.output_dir),
            state_path=Path(args.state),
            expected_dates_path=Path(args.expected_dates) if args.expected_dates else None,
            config=_position_monitor_config(args),
            live=args.live,
            webhook_url=args.webhook_url,
        )
        print(output_path)
    elif args.command == "position-monitor":
        output_path = generate_position_monitor_loop(
            positions_path=Path(args.positions),
            output_dir=Path(args.output_dir),
            state_path=Path(args.state),
            expected_dates_path=Path(args.expected_dates) if args.expected_dates else None,
            config=_position_monitor_config(args),
            live=args.live,
            webhook_url=args.webhook_url,
            interval_seconds=args.interval_seconds,
            market_start=_parse_time(args.market_start),
            market_end=_parse_time(args.market_end),
        )
        print(output_path)
    elif args.command == "market-monitor-once":
        positions_path = Path(args.positions) if args.positions else None
        output_path = generate_market_monitor_once(
            output_dir=Path(args.output_dir),
            state_path=Path(args.state),
            expected_dates_path=Path(args.expected_dates) if args.expected_dates else None,
            positions_path=positions_path,
            config=_market_monitor_config(args),
            live=args.live,
            webhook_url=args.webhook_url,
        )
        print(output_path)
    elif args.command == "market-monitor":
        positions_path = Path(args.positions) if args.positions else None
        output_path = generate_market_monitor_loop(
            output_dir=Path(args.output_dir),
            state_path=Path(args.state),
            expected_dates_path=Path(args.expected_dates) if args.expected_dates else None,
            positions_path=positions_path,
            config=_market_monitor_config(args),
            live=args.live,
            webhook_url=args.webhook_url,
            interval_seconds=args.interval_seconds,
            market_start=_parse_time(args.market_start),
            market_end=_parse_time(args.market_end),
        )
        print(output_path)
    elif args.command == "push-status":
        output_path = generate_push_status(
            Path(args.output),
            reports_dir=Path(args.reports_dir),
            runs_dir=Path(args.runs_dir),
            expected_dates_path=Path(args.expected_dates),
            positions_path=Path(args.positions),
            webhook_url=args.webhook_url,
        )
        print(output_path)
    elif args.command == "push-test":
        output_path = generate_push_test(
            Path(args.output_dir),
            webhook_url=args.webhook_url,
            live=args.live,
        )
        print(output_path)
    elif args.command == "push-windows-task-status":
        output_path = generate_windows_task_status(Path(args.output))
        print(output_path)
    elif args.command == "push-go-live":
        output_path = generate_push_go_live(
            Path(args.output),
            runs_dir=Path(args.runs_dir),
            expected_dates_path=Path(args.expected_dates),
        )
        print(output_path)
    elif args.command == "push-activation":
        output_path = generate_push_activation(
            Path(args.output),
            runs_dir=Path(args.runs_dir),
            reports_dir=Path(args.reports_dir),
            expected_dates_path=Path(args.expected_dates),
            project_dir=Path(args.project_dir) if args.project_dir else Path.cwd(),
        )
        print(output_path)
    else:
        parser.print_help()


def generate_summary(as_of: dt.date, output_dir: Path) -> Path:
    archive_dir = report_archive_dir(output_dir, as_of)
    archive_dir.mkdir(parents=True, exist_ok=True)
    dataset = fetch_market_dataset(as_of)
    summary = summarize_market(dataset)
    report = build_summary_report(summary)
    output_path = archive_dir / f"summary_{as_of.isoformat()}.md"
    output_path.write_text(report, encoding="utf-8")
    return output_path


def generate_daily_pack(
    as_of: dt.date,
    output_dir: Path,
    positions_path: Path | None = None,
    split: bool = False,
    require_positions: bool = False,
    focus_positions: bool = True,
) -> Path:
    archive_dir = report_archive_dir(output_dir, as_of)
    archive_dir.mkdir(parents=True, exist_ok=True)
    if require_positions and not focus_positions:
        raise ValueError("INVALID_INPUT_SCOPE: --require-positions 不能和 --ignore-positions 同时使用")
    if require_positions and positions_path is None:
        raise ValueError("STRICT_REQUIRE_POSITIONS: 严格模式要求提供 --positions")
    readiness_path = positions_path if positions_path else Path("data/positions.csv")
    readiness_result = check_inputs(readiness_path, focus_positions=focus_positions)
    if require_positions and not readiness_result.ready:
        raise ValueError(f"INPUTS_NOT_READY: {', '.join(readiness_result.errors)}")
    dataset = fetch_market_dataset(as_of, include_stocks=True, include_news=True)
    summary = summarize_market(dataset)
    positions = load_positions(positions_path) if positions_path and focus_positions else []
    output_path = archive_dir / f"daily_pack_{as_of.isoformat()}.md"
    if split:
        sections = build_daily_report_sections(
            summary,
            positions=positions,
            news_items=dataset.news_items,
            focus_positions=focus_positions,
        )
        names = {
            "daily_pack": output_path,
            "push_preview": archive_dir / f"push_preview_{as_of.isoformat()}.md",
            "push_contract": archive_dir / f"push_contract_{as_of.isoformat()}.md",
            "readiness": archive_dir / f"readiness_{as_of.isoformat()}.md",
            "intraday": archive_dir / f"intraday_{as_of.isoformat()}.md",
            "positions": archive_dir / f"positions_{as_of.isoformat()}.md",
            "watchlist": archive_dir / f"watchlist_{as_of.isoformat()}.md",
            "news": archive_dir / f"news_{as_of.isoformat()}.md",
            "evening": archive_dir / f"evening_{as_of.isoformat()}.md",
        }
        sections["readiness"] = render_readiness_report(readiness_result)
        for key, path in names.items():
            path.write_text(sections[key], encoding="utf-8")
        audit_daily_report(sections["daily_pack"], require_positions=require_positions)
        write_manifest_report(
            as_of,
            archive_dir,
            strict_ready=readiness_result.ready,
            readiness_errors=readiness_result.errors,
        )
    else:
        report = build_daily_pack_report(
            summary,
            positions=positions,
            news_items=dataset.news_items,
            focus_positions=focus_positions,
        )
        audit_daily_report(report, require_positions=require_positions)
        output_path.write_text(report, encoding="utf-8")
    return output_path


def generate_readiness_report(
    as_of: dt.date,
    output_dir: Path,
    positions_path: Path,
    focus_positions: bool = True,
) -> Path:
    archive_dir = report_archive_dir(output_dir, as_of)
    archive_dir.mkdir(parents=True, exist_ok=True)
    result = check_inputs(positions_path, focus_positions=focus_positions)
    output_path = archive_dir / f"readiness_{as_of.isoformat()}.md"
    output_path.write_text(render_readiness_report(result), encoding="utf-8")
    if not result.ready:
        raise ValueError(f"INPUTS_NOT_READY: {', '.join(result.errors)}")
    return output_path


def import_positions_file(source: Path, output: Path, encoding: str = "utf-8-sig") -> Path:
    return import_positions_csv(source, output, encoding=encoding)


def watch_positions_exports(
    source_dir: Path,
    output: Path,
    pattern: str = "*.csv",
    encoding: str = "utf-8-sig",
    interval: float = 10.0,
    once: bool = False,
) -> Path:
    if interval <= 0:
        raise ValueError("监听间隔必须大于 0")
    last_source: Path | None = None
    while True:
        source = import_latest_positions_export(source_dir, output, pattern=pattern, encoding=encoding)
        if source != last_source:
            print(f"已同步持仓: {source} -> {output}", flush=True)
            last_source = source
        if once:
            return source
        time.sleep(interval)


def generate_run_audit(
    as_of: dt.date,
    reports_dir: Path,
    output_dir: Path,
    command: str | None = None,
) -> Path:
    return write_run_audit_report(as_of, reports_dir, output_dir, command=command)


def generate_run_summary(
    runs_dir: Path,
    target_days: int = 10,
    expected_dates_path: Path | None = None,
) -> Path:
    expected_dates = load_expected_dates(expected_dates_path) if expected_dates_path else None
    return write_run_summary_report(runs_dir, target_days=target_days, expected_dates=expected_dates)


def generate_trial_gate(runs_dir: Path) -> Path:
    return write_stage_gate_report(runs_dir)


def generate_trial_status(
    runs_dir: Path,
    expected_dates_path: Path,
    reports_dir: Path = Path("reports"),
    target_days: int = 10,
    focus_positions: bool = True,
) -> Path:
    return write_trial_status_report(
        runs_dir,
        expected_dates_path,
        reports_dir=reports_dir,
        target_days=target_days,
        focus_positions=focus_positions,
    )


def add_expected_trading_dates(path: Path, date_values: list[str]) -> Path:
    dates = [_parse_expected_date(value) for value in date_values]
    return add_expected_dates(path, dates)


def generate_push_schedule(
    output_path: Path,
    reports_dir: Path = Path("reports"),
    runs_dir: Path = Path("planning/runs"),
    target_days: int = 10,
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    positions_path: Path | None = None,
    focus_positions: bool = True,
) -> Path:
    return write_push_schedule_report(
        output_path,
        reports_dir=reports_dir,
        runs_dir=runs_dir,
        target_days=target_days,
        expected_dates_path=expected_dates_path,
        positions_path=positions_path,
        focus_positions=focus_positions,
    )


def generate_push_send(
    as_of: dt.date,
    reports_dir: Path,
    slot: str,
    output_dir: Path,
    live: bool = False,
    webhook_url: str | None = None,
) -> Path:
    return send_push_slot(
        as_of,
        reports_dir,
        slot,
        output_dir,
        live=live,
        webhook_url=webhook_url,
    )


def generate_push_run(
    as_of: dt.date,
    slot: str,
    reports_dir: Path,
    output_dir: Path,
    positions_path: Path | None = None,
    require_positions: bool = False,
    focus_positions: bool = True,
    live: bool = False,
    webhook_url: str | None = None,
) -> Path:
    if slot != "pre_open":
        generate_daily_pack(
            as_of,
            reports_dir,
            positions_path=positions_path,
            split=True,
            require_positions=require_positions,
            focus_positions=focus_positions,
        )
    return generate_push_send(
        as_of,
        reports_dir,
        slot,
        output_dir,
        live=live,
        webhook_url=webhook_url,
    )


def generate_push_daily(
    slot: str,
    reports_dir: Path,
    output_dir: Path,
    expected_dates_path: Path,
    positions_path: Path | None = None,
    require_positions: bool = False,
    focus_positions: bool = True,
    live: bool = False,
    webhook_url: str | None = None,
    today: dt.date | None = None,
) -> Path:
    current_date = today if today is not None else dt.date.today()
    expected_dates = load_expected_dates(expected_dates_path)
    as_of = _push_daily_report_date(slot, expected_dates, current_date)
    return generate_push_run(
        as_of,
        slot,
        reports_dir,
        output_dir,
        positions_path=positions_path,
        require_positions=require_positions,
        focus_positions=focus_positions,
        live=live,
        webhook_url=webhook_url,
    )


def generate_windows_task_script(
    output_path: Path,
    project_dir: Path,
    reports_dir: Path = Path("reports"),
    runs_dir: Path = Path("planning/runs"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    positions_path: Path | None = None,
    python_command: str = "python",
    focus_positions: bool = True,
    live: bool = False,
) -> Path:
    return write_windows_task_script(
        output_path,
        project_dir=project_dir,
        reports_dir=reports_dir,
        runs_dir=runs_dir,
        expected_dates_path=expected_dates_path,
        positions_path=positions_path,
        python_command=python_command,
        focus_positions=focus_positions,
        live=live,
    )


def generate_position_monitor_once(
    positions_path: Path = Path("data/positions.csv"),
    output_dir: Path = Path("planning/runs"),
    state_path: Path = Path("planning/runs/position_monitor_state.json"),
    expected_dates_path: Path | None = Path("planning/runs/expected_dates.txt"),
    config: PositionMonitorConfig = PositionMonitorConfig(),
    live: bool = False,
    webhook_url: str | None = None,
    now: dt.datetime | None = None,
) -> Path:
    return run_position_monitor_once(
        positions_path,
        output_dir,
        state_path,
        expected_dates_path=expected_dates_path,
        config=config,
        live=live,
        webhook_url=webhook_url,
        now=now,
    )


def generate_position_monitor_loop(
    positions_path: Path = Path("data/positions.csv"),
    output_dir: Path = Path("planning/runs"),
    state_path: Path = Path("planning/runs/position_monitor_state.json"),
    expected_dates_path: Path | None = Path("planning/runs/expected_dates.txt"),
    config: PositionMonitorConfig = PositionMonitorConfig(),
    live: bool = False,
    webhook_url: str | None = None,
    interval_seconds: int = 60,
    market_start: dt.time = dt.time(9, 30),
    market_end: dt.time = dt.time(15, 0),
) -> Path:
    return run_position_monitor_loop(
        positions_path,
        output_dir,
        state_path,
        expected_dates_path=expected_dates_path,
        config=config,
        live=live,
        webhook_url=webhook_url,
        interval_seconds=interval_seconds,
        market_start=market_start,
        market_end=market_end,
    )


def generate_market_monitor_once(
    output_dir: Path = Path("planning/runs"),
    state_path: Path = Path("planning/runs/market_monitor_state.json"),
    expected_dates_path: Path | None = Path("planning/runs/expected_dates.txt"),
    positions_path: Path | None = Path("data/positions.csv"),
    config: MarketMonitorConfig = MarketMonitorConfig(),
    live: bool = False,
    webhook_url: str | None = None,
    now: dt.datetime | None = None,
) -> Path:
    return run_market_monitor_once(
        output_dir,
        state_path,
        expected_dates_path=expected_dates_path,
        positions_path=positions_path,
        config=config,
        live=live,
        webhook_url=webhook_url,
        now=now,
    )


def generate_market_monitor_loop(
    output_dir: Path = Path("planning/runs"),
    state_path: Path = Path("planning/runs/market_monitor_state.json"),
    expected_dates_path: Path | None = Path("planning/runs/expected_dates.txt"),
    positions_path: Path | None = Path("data/positions.csv"),
    config: MarketMonitorConfig = MarketMonitorConfig(),
    live: bool = False,
    webhook_url: str | None = None,
    interval_seconds: int = 60,
    market_start: dt.time = dt.time(9, 30),
    market_end: dt.time = dt.time(15, 0),
) -> Path:
    return run_market_monitor_loop(
        output_dir,
        state_path,
        expected_dates_path=expected_dates_path,
        positions_path=positions_path,
        config=config,
        live=live,
        webhook_url=webhook_url,
        interval_seconds=interval_seconds,
        market_start=market_start,
        market_end=market_end,
    )


def generate_push_status(
    output_path: Path,
    reports_dir: Path = Path("reports"),
    runs_dir: Path = Path("planning/runs"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    positions_path: Path = Path("data/positions.csv"),
    today: dt.date | None = None,
    webhook_url: str | None = None,
) -> Path:
    return write_push_status_report(
        output_path,
        reports_dir=reports_dir,
        runs_dir=runs_dir,
        expected_dates_path=expected_dates_path,
        positions_path=positions_path,
        today=today,
        webhook_url=webhook_url,
    )


def generate_push_test(
    output_dir: Path = Path("planning/runs"),
    webhook_url: str | None = None,
    live: bool = False,
    now: dt.datetime | None = None,
) -> Path:
    return send_push_channel_test(
        output_dir,
        webhook_url=webhook_url,
        live=live,
        now=now,
    )


def generate_windows_task_status(output_path: Path = Path("planning/runs/push_windows_task_status.md")) -> Path:
    return write_windows_task_status_report(output_path)


def generate_push_go_live(
    output_path: Path = Path("planning/runs/push_go_live.md"),
    runs_dir: Path = Path("planning/runs"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
) -> Path:
    return write_push_go_live_report(output_path, runs_dir=runs_dir, expected_dates_path=expected_dates_path)


def generate_push_activation(
    output_path: Path = Path("planning/runs/push_activation.md"),
    runs_dir: Path = Path("planning/runs"),
    reports_dir: Path = Path("reports"),
    expected_dates_path: Path = Path("planning/runs/expected_dates.txt"),
    project_dir: Path = Path("."),
) -> Path:
    return write_push_activation_report(
        output_path,
        runs_dir=runs_dir,
        reports_dir=reports_dir,
        expected_dates_path=expected_dates_path,
        project_dir=project_dir,
    )


def _push_daily_report_date(slot: str, expected_dates: list[dt.date], today: dt.date) -> dt.date:
    if slot == "pre_open":
        previous_dates = [date for date in expected_dates if date < today]
        if not previous_dates:
            raise ValueError(f"PUSH_DAILY_NO_PREVIOUS_EXPECTED_DATE:TODAY:{today.isoformat()}")
        return previous_dates[-1]
    if today not in expected_dates:
        raise ValueError(f"PUSH_DAILY_DATE_NOT_EXPECTED:{today.isoformat()}")
    return today


def _parse_expected_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"EXPECTED_DATE_INVALID:{value}") from exc


def _add_position_monitor_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--positions", default="data/positions.csv", help="真实持仓 CSV 路径")
    parser.add_argument("--output-dir", default="planning/runs", help="监控报告输出目录")
    parser.add_argument("--state", default="planning/runs/position_monitor_state.json", help="提醒冷却状态文件")
    parser.add_argument("--expected-dates", default="planning/runs/expected_dates.txt", help="期望交易日清单")
    parser.add_argument("--stop-loss-pct", type=float, default=-5.0, help="浮亏触发阈值，默认 -5")
    parser.add_argument("--take-profit-pct", type=float, default=10.0, help="浮盈触发阈值，默认 10")
    parser.add_argument("--intraday-drop-pct", type=float, default=-5.0, help="日内跌幅触发阈值，默认 -5")
    parser.add_argument("--cooldown-minutes", type=int, default=30, help="同一股票同一触发类型冷却分钟数")
    parser.add_argument("--webhook-url", default=None, help="飞书自定义机器人 webhook；不填则读取 FEISHU_WEBHOOK_URL")
    parser.add_argument("--live", action="store_true", help="真实发送；不加时只写 dry-run 报告")


def _position_monitor_config(args: argparse.Namespace) -> PositionMonitorConfig:
    return PositionMonitorConfig(
        stop_loss_pct=args.stop_loss_pct,
        take_profit_pct=args.take_profit_pct,
        intraday_drop_pct=args.intraday_drop_pct,
        cooldown_minutes=args.cooldown_minutes,
    )


def _add_market_monitor_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--positions", default=None, help="真实持仓 CSV 路径；不提供则只监控市场和候选股")
    parser.add_argument("--output-dir", default="planning/runs", help="监控报告输出目录")
    parser.add_argument("--state", default="planning/runs/market_monitor_state.json", help="提醒冷却状态文件")
    parser.add_argument("--expected-dates", default="planning/runs/expected_dates.txt", help="期望交易日清单")
    parser.add_argument("--capital-outflow-threshold", type=float, default=-500000000.0, help="板块资金出逃阈值，默认 -5 亿")
    parser.add_argument("--capital-outflow-drop-pct", type=float, default=-1.0, help="资金出逃同时要求板块跌幅，默认 -1")
    parser.add_argument("--cooldown-minutes", type=int, default=30, help="同一对象同一触发类型冷却分钟数")
    parser.add_argument("--webhook-url", default=None, help="飞书自定义机器人 webhook；不填则读取 FEISHU_WEBHOOK_URL")
    parser.add_argument("--live", action="store_true", help="真实发送；不加时只写 dry-run 报告")


def _market_monitor_config(args: argparse.Namespace) -> MarketMonitorConfig:
    return MarketMonitorConfig(
        capital_outflow_threshold=args.capital_outflow_threshold,
        capital_outflow_drop_pct=args.capital_outflow_drop_pct,
        cooldown_minutes=args.cooldown_minutes,
    )


def _parse_time(value: str) -> dt.time:
    try:
        return dt.time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"TIME_INVALID:{value}") from exc


def generate_trial_run(
    as_of: dt.date,
    reports_dir: Path,
    runs_dir: Path,
    positions_path: Path | None = None,
    require_positions: bool = False,
    focus_positions: bool = True,
    target_days: int = 10,
    expected_dates_path: Path | None = None,
    allow_rerun: bool = False,
    today: dt.date | None = None,
) -> Path:
    current_date = today if today is not None else dt.date.today()
    if as_of > current_date:
        raise ValueError(f"TRIAL_RUN_FUTURE_DATE:{as_of.isoformat()}:TODAY:{current_date.isoformat()}")
    record_path = runs_dir / f"run_audit_{as_of.isoformat()}.md"
    if record_path.exists() and not allow_rerun:
        raise ValueError(f"TRIAL_RUN_RECORD_EXISTS:{as_of.isoformat()}")
    command = _daily_pack_source_command(as_of, reports_dir, positions_path, require_positions, focus_positions)
    expected_dates: list[dt.date] | None = None
    try:
        if expected_dates_path is not None:
            expected_dates = load_expected_dates(expected_dates_path)
            if as_of not in expected_dates:
                raise ValueError(f"TRIAL_RUN_DATE_NOT_EXPECTED:{as_of.isoformat()}")
        generate_daily_pack(
            as_of,
            reports_dir,
            positions_path=positions_path,
            split=True,
            require_positions=require_positions,
            focus_positions=focus_positions,
        )
    except Exception as exc:
        write_failed_run_audit_report(as_of, runs_dir, exc, command=command)
        write_run_summary_report(runs_dir, target_days=target_days, expected_dates=expected_dates)
        write_stage_gate_report(runs_dir)
        raise
    generate_run_audit(
        as_of,
        reports_dir,
        runs_dir,
        command=command,
    )
    summary_path = write_run_summary_report(runs_dir, target_days=target_days, expected_dates=expected_dates)
    write_stage_gate_report(runs_dir)
    return summary_path


def generate_trial_run_next(
    reports_dir: Path,
    runs_dir: Path,
    expected_dates_path: Path,
    positions_path: Path | None = None,
    require_positions: bool = False,
    focus_positions: bool = True,
    target_days: int = 10,
    today: dt.date | None = None,
) -> Path:
    expected_dates = load_expected_dates(expected_dates_path)
    as_of = select_next_expected_date(runs_dir, expected_dates)
    return generate_trial_run(
        as_of,
        reports_dir,
        runs_dir,
        positions_path=positions_path,
        require_positions=require_positions,
        focus_positions=focus_positions,
        target_days=target_days,
        expected_dates_path=expected_dates_path,
        today=today,
    )


def _daily_pack_source_command(
    as_of: dt.date,
    reports_dir: Path,
    positions_path: Path | None,
    require_positions: bool,
    focus_positions: bool,
) -> str:
    parts = [
        "python -m personal_quant.cli daily-pack",
        f"--as-of {as_of.isoformat()}",
        f"--output-dir {reports_dir}",
        "--split",
    ]
    if positions_path is not None:
        parts.append(f"--positions {positions_path}")
    if require_positions:
        parts.append("--require-positions")
    if not focus_positions:
        parts.append("--ignore-positions")
    return " ".join(parts)


if __name__ == "__main__":
    main()
