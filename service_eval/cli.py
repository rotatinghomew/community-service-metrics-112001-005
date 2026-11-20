"""Command line entry point.

    python3 -m service_eval serve   --sources fixtures --state data/state.json --port 8000
    python3 -m service_eval check   --sources fixtures
    python3 -m service_eval analyze --sources fixtures --state data/state.json --out report.json
    python3 -m service_eval freeze  --sources fixtures --state data/state.json --protocol plan.json --reason "初次冻结"

Freezing requires a protocol JSON file; without it ``analyze`` refuses to run,
because the outcome measure and comparison window must be fixed before seeing
how any grid did.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .domain import SourceValidationError, check_sources
from .engine import analyze
from .protocol import ProtocolError, StateStore, draft_protocol, validate_protocol
from .sources import SourceError, load_directory
from .server import make_server


def _load_tables(sources: str):
    return load_directory(sources)


def cmd_check(args: argparse.Namespace) -> int:
    bundle = _load_tables(args.sources)
    issues = bundle.check()
    print(json.dumps({"counts": bundle.fingerprints(), "issues": issues}, ensure_ascii=False, indent=2))
    return 0 if not any(item["severity"] == "error" for item in issues) else 1


def cmd_freeze(args: argparse.Namespace) -> int:
    bundle = _load_tables(args.sources)
    spec_path = Path(args.protocol)
    if not spec_path.exists():
        print(f"找不到方案文件 {spec_path}", file=sys.stderr)
        return 2
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    store = StateStore(args.state)
    record = store.freeze(spec, args.reason, bundle.tables)
    print(json.dumps({"version": record["version"], "changes": record["changes_from_previous"]}, ensure_ascii=False, indent=2))
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    bundle = _load_tables(args.sources)
    print(json.dumps(draft_protocol(bundle.tables), ensure_ascii=False, indent=2))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    bundle = _load_tables(args.sources)
    store = StateStore(args.state)
    data = store.load()
    if args.protocol_version:
        record = store.protocol_version(args.protocol_version)
    else:
        record = store.active_protocol(data)
    if record is None:
        print("还没有冻结方案：先用 freeze 固定结果口径和比较时段", file=sys.stderr)
        return 2
    report = analyze(bundle.tables, record["spec"], record["version"])
    report["quality"]["issues"] = report["quality"]["issues"] + [
        item for item in bundle.check()
        if (item["severity"], item["message"]) not in {
            (q["severity"], q["message"]) for q in report["quality"]["issues"]
        }
    ]
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.save:
        saved = store.save_report(report)
        print(f"已保存报告 {saved['id']}")
    summary = {
        "protocol_version": record["version"],
        "estimate": {
            key: report["estimate"][key]
            for key in ("status", "status_label", "value_pct", "ci_low_pct", "ci_high_pct", "ci_method")
        },
        "conclusion": report["conclusion"]["text"],
        "can_conclude": report["conclusion"]["can_conclude"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    server = make_server(args.host, args.port, args.sources, args.state)
    print(f"服务已启动：http://{args.host}:{args.port}/  （Ctrl+C 停止）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="service_eval", description="分阶段服务措施效果评估")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="校验资料")
    check.add_argument("--sources", default="fixtures")
    check.set_defaults(func=cmd_check)

    draft = sub.add_parser("draft", help="输出方案草案")
    draft.add_argument("--sources", default="fixtures")
    draft.set_defaults(func=cmd_draft)

    freeze = sub.add_parser("freeze", help="冻结或修订方案")
    freeze.add_argument("--sources", default="fixtures")
    freeze.add_argument("--state", default="data/state.json")
    freeze.add_argument("--protocol", required=True)
    freeze.add_argument("--reason", required=True)
    freeze.set_defaults(func=cmd_freeze)

    analyze_cmd = sub.add_parser("analyze", help="按冻结方案重算")
    analyze_cmd.add_argument("--sources", default="fixtures")
    analyze_cmd.add_argument("--state", default="data/state.json")
    analyze_cmd.add_argument("--protocol-version", type=int, default=None)
    analyze_cmd.add_argument("--out", default="")
    analyze_cmd.add_argument("--save", action="store_true")
    analyze_cmd.set_defaults(func=cmd_analyze)

    serve = sub.add_parser("serve", help="启动网页")
    serve.add_argument("--sources", default="fixtures")
    serve.add_argument("--state", default="data/state.json")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (SourceError, SourceValidationError, ProtocolError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
