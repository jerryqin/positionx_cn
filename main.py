from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.broker import _broker_stage_trace, _broker_traced_call
if __name__ == "__main__" and len(sys.argv)>1 and sys.argv[1] in ("check","reconcile"):
    _broker_stage_trace("worker_start", "ENTER")

from core.config import Settings
from core.logging_setup import setup_logging
from core.service import TradingService
from core.scheduler import DailyScheduler


def pp(x):
    print(json.dumps(x, ensure_ascii=False, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description="PositionX Minimal Live")
    parser.add_argument("command", choices=["gui","check","plan","execute","dry-run","exact","reconcile","daemon","kill-on","kill-off","init-db"])
    parser.add_argument("--date", default="")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--target-shares", type=int, default=None)
    parser.add_argument("--side", choices=["BUY", "SELL"], default=None)
    parser.add_argument("--volume", type=int, default=None)
    parser.add_argument("--submit", action="store_true", help="exact命令：走真实提交路径；仍受live_enabled/Pilot/RiskGate硬保护")
    args = parser.parse_args()

    s = Settings(args.config)
    setup_logging(s.get("app.log_dir", "logs"))

    if args.command == "gui":
        from gui.app import run_gui
        return run_gui(args.config)

    svc = _broker_traced_call("service_init", TradingService, args.config)
    try:
        if args.command == "init-db":
            print("DB initialized:", s.get("app.db_path")); return
        if args.command == "kill-on":
            svc.risk.set_kill_switch(True); print("Kill Switch = ON"); return
        if args.command == "kill-off":
            svc.risk.set_kill_switch(False); print("Kill Switch = OFF"); return
        if args.command == "check": pp(svc.readiness_check())
        elif args.command == "plan": pp(svc.prepare_plan(args.date or None))
        elif args.command == "execute": pp(svc.execute_plan(args.date or None, dry_run=False))
        elif args.command == "dry-run": pp(svc.execute_plan(args.date or None, dry_run=True))
        elif args.command == "exact":
            if not args.symbol:
                parser.error("exact 命令必须指定 --symbol")
            has_target = args.target_shares is not None
            has_volume = args.side is not None or args.volume is not None
            if has_target == has_volume:
                parser.error("exact 命令必须且只能指定 --target-shares 或 --side + --volume")
            if has_volume and (args.side is None or args.volume is None):
                parser.error("--side 与 --volume 必须同时指定")
            if args.target_shares is not None and args.target_shares < 0:
                parser.error("--target-shares 不能为负数")
            if args.volume is not None and args.volume <= 0:
                parser.error("--volume 必须为正整数")
            pp(svc.execute_quantity(
                args.symbol,
                target_shares=args.target_shares,
                side=args.side,
                volume=args.volume,
                trade_date=args.date or None,
                dry_run=not args.submit,
            ))
        elif args.command == "reconcile": pp(svc.reconcile())
        elif args.command == "daemon":
            scheduler = DailyScheduler(svc)
            poll = int(s.get("schedule.poll_seconds", 5))
            print(f"daemon started, poll={poll}s; Ctrl+C退出")
            while True:
                try:
                    out = scheduler.tick()
                    if out: pp(out)
                except Exception as e:
                    svc.db.event("ERROR", "SCHEDULER", str(e))
                    print("scheduler error:", e)
                time.sleep(poll)
    except BaseException as primary:
        try:
            _broker_stage_trace("worker_exception", "EXCEPTION", error=primary)
        except Exception as trace_error:
            raise primary from trace_error
        raise
    finally:
        svc.disconnect()
        _broker_stage_trace("worker_exit", "RETURNED")


if __name__ == "__main__":
    main()
