"""keyfleet 命令行：check / ask / status / report。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .core import AllEndpointsFailed, BudgetExceeded, Fleet, HUMAN_KIND, LocalGateBlocked


def _load(args):
    if not os.path.exists(os.path.expanduser(args.config)):
        sys.exit(f"配置文件不存在: {args.config}")
    return Fleet.from_file(args.config)


def cmd_check(args):
    f = _load(args)
    rows = f.check(workers=args.workers)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    width = max(len(r["endpoint"]) for r in rows)
    for r in rows:
        mark = "OK  " if r["ok"] else "FAIL"
        print(f"{mark} {r['endpoint']:<{width}}  {r['key']:<14}  {r.get('ms', 0):>5}ms  {r['detail']}")

    # 一句结论 + 分诊建议。体检报告的用处不在表格，而在"接下来该删哪把、等哪把"。
    counts = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    print()
    print("合计: " + " / ".join(f"{HUMAN_KIND.get(k, k)} {v}" for k, v in sorted(counts.items())))
    hints = {
        "auth": "key 无效或被吊销：从配置里删掉，别留着每次先撞一遍 401。",
        "quota": "额度/限流：等冷却或充值；如果是免费额度，注意别把新 key 也搭进去。",
        "local-gate": "本机出口被挡：换 key 无效，先确认代理/网关。",
        "unreachable": "连不上：先查网络与 base_url，不是 key 的问题。",
        "bad-request": "请求被拒：多半是模型名不在 /models 里，核对 model 字段。",
    }
    for kind in ("local-gate", "auth", "quota", "unreachable", "bad-request"):
        if counts.get(kind):
            print(f"  · {hints[kind]}")
    return 0 if all(r["ok"] for r in rows) else 1


def cmd_status(args):
    f = _load(args)
    st = f.status()
    print(f"舰队今日花费: ${st['fleet_spent_today_usd']:.6f}"
          + (f"  / 上限 ${st['daily_cap_usd']}" if st.get("daily_cap_usd") else ""))
    for e in st["endpoints"]:
        cap = "无" if e["remaining_today_usd"] is None else f"${e['remaining_today_usd']:.6f}"
        cd = f" 冷却{e['cooling_down_sec']}s" if e["cooling_down_sec"] else ""
        print(f"  {'可用' if e['usable'] else '停用'}  {e['name']:<16} {e['key_hint']:<14} "
              f"今日 ${e['spent_today_usd']:.6f} 余 {cap}{cd}")
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=1))
    return 0


def cmd_report(args):
    f = _load(args)
    rows = f.ledger.report(days=args.days)
    if not rows:
        print("账本还是空的。")
        return 0
    print(f"{'日期':<12}{'端点':<16}{'调用':>5}{'成功':>5}{'花费USD':>12}{'输入tok':>10}{'输出tok':>10}")
    for r in rows:
        print(f"{r['day']:<12}{r['endpoint']:<16}{r['calls']:>5}{r['ok'] or 0:>5}"
              f"{r['cost_usd']:>12.6f}{r['prompt_tokens']:>10}{r['completion_tokens']:>10}")
    return 0


def cmd_ask(args):
    f = _load(args)
    msgs = [{"role": "user", "content": args.prompt}]
    try:
        r = f.chat(msgs, model=args.model, max_tokens=args.max_tokens)
    except BudgetExceeded as e:
        print(f"已拒绝（预算）: {e}", file=sys.stderr)
        return 3
    except LocalGateBlocked as e:
        print(f"本机出口被挡住，换 key 无用，等窗口: {e.reason}", file=sys.stderr)
        return 4
    except AllEndpointsFailed as e:
        print(f"全部端点失败: {e}", file=sys.stderr)
        return 2
    print(r.text)
    if args.verbose:
        print(f"\n[端点 {r.endpoint} · 模型 {r.model} · 尝试 {r.attempts} 次 · "
              f"{r.prompt_tokens}+{r.completion_tokens} tok · ${r.cost_usd:.6f}]", file=sys.stderr)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="keyfleet", description="多把 OpenAI 兼容 key 的自动故障转移与花费上限")
    ap.add_argument("-c", "--config", default="~/.keyfleet/fleet.json", help="配置文件路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check", help="并发体检每把 key：可用 / 无效 / 额度尽 / 出口被挡")
    p.add_argument("-j", "--json", action="store_true", help="输出 JSON（便于喂给监控）")
    p.add_argument("-w", "--workers", type=int, default=8, help="并发数（默认 8）")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("status", help="今日花费 / 剩余额度 / 冷却状态")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", help="按天按端点汇总账本")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("ask", help="问一句话，自动挑可用的 key")
    p.add_argument("prompt")
    p.add_argument("-m", "--model")
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_ask)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
