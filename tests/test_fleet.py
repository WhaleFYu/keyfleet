"""用注入式假传输层测试，不需要监听端口（也能在严格沙箱里跑）。

Fleet 的 opener 参数就是注入点：任何 `opener(req, timeout=...) -> 响应对象` 都能用。
"""
import io, json, os, sys, tempfile, time, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from keyfleet import Endpoint, Fleet, Ledger, AllEndpointsFailed, BudgetExceeded
from keyfleet.core import _FetchError, LocalGateBlocked, classify, is_local_gate

OK_BODY = {"model": "stub", "choices": [{"message": {"content": "hello from stub"}}],
           "usage": {"prompt_tokens": 100, "completion_tokens": 50}}


class Resp:
    def __init__(self, status, body):
        self.status = status
        self._b = (json.dumps(body) if not isinstance(body, str) else body).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Fake:
    """plan: {key_tail 或 "*": [ (status, body), ... ]}，最后一项常驻。

    key_tail 用 key 的后 6 位匹配，这样同一个 URL 的不同 key 可以有不同剧本。
    """

    def __init__(self, plan=None):
        self.plan = plan or {}
        self.calls = []            # (url, body_dict, auth_header)

    def __call__(self, req, timeout=None):
        url = req.full_url
        payload = json.loads(req.data) if req.data else {}
        auth = req.get_header("Authorization") or ""
        self.calls.append((url, payload, auth))
        seq = None
        for tail, s in self.plan.items():
            if tail == "*" or auth.endswith(tail):
                seq = s
                break
        if seq is None:
            return Resp(200, OK_BODY)
        status, body = seq.pop(0) if len(seq) > 1 else seq[0]
        if status >= 400:
            raise urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(str(body).encode()))
        return Resp(status, body)


def fresh():
    return Ledger(os.path.join(tempfile.mkdtemp(), "ledger.db"))


CHECKS = []


def check(cond, label):
    assert cond, f"FAIL: {label}"
    CHECKS.append(label)
    print(f"  ok - {label}")


def main():
    # 1. 基本调用 + 记账
    led = fresh()
    t = Fake()
    f = Fleet([Endpoint("a", "https://x/v1", "sk-aaaaaaaaaaaa", "m1", price_in=0.1, price_out=0.4)],
              ledger=led, opener=t)
    r = f.chat([{"role": "user", "content": "hi"}])
    check(r.text == "hello from stub", "返回文本")
    check(abs(r.cost_usd - (100 * 0.1 + 50 * 0.4) / 1e6) < 1e-12, "成本按配置单价计算")
    check(abs(led.spend("a") - r.cost_usd) < 1e-12, "账本累加一致")
    check(t.calls[0][2] == "Bearer sk-aaaaaaaaaaaa", "鉴权头正确")
    check(r.model == "stub", "模型名取自响应")

    # 2. key 不外泄
    check("sk-aaaaaaaaaaaa" not in repr(f.endpoints[0]), "repr 不含明文 key")
    check("sk-aaaaaaaaaaaa" not in json.dumps([e.to_public() for e in f.endpoints]), "to_public 不含明文 key")
    check("sk-aaaaaaaaaaaa" not in json.dumps(f.status()), "status 不含明文 key")

    # 3. 429 换 key
    led = fresh()
    t = Fake({"111111": [(429, "rate limit exceeded")]})
    e1 = Endpoint("limited", "https://x/v1", "sk-111111111111", "m1", priority=0)
    e2 = Endpoint("backup", "https://x/v1", "sk-222222222222", "m1", priority=10)
    f = Fleet([e1, e2], ledger=led, opener=t)
    r = f.chat([{"role": "user", "content": "x"}])
    check(r.endpoint == "backup", "429 后切到备用 key")
    check(e1.cooled_down(), "429 的 key 进入冷却")
    check(led.calls_today("limited") == 1 and led.calls_today("backup") == 1, "两次调用都入账")
    check([e.name for e in f.candidates()] == ["backup"], "冷却中的 key 不再被选中")
    check(f.status()["endpoints"][0]["cooling_down_sec"] > 0, "status 报告剩余冷却秒数")

    # 4. 401 → 长冷却
    led = fresh()
    t = Fake({"333333": [(401, '{"error":"invalid api key"}')]})
    ea = Endpoint("revoked", "https://x/v1", "sk-333333333333", "m1", priority=0)
    eb = Endpoint("good", "https://x/v1", "sk-444444444444", "m1", priority=10)
    f = Fleet([ea, eb], ledger=led, opener=t)
    f.chat([{"role": "user", "content": "x"}])
    check(ea.cooldown_until - time.time() > 5 * 3600, "401 → 停用约 6 小时")

    # 5. 单 key 日上限
    led = fresh()
    e = Endpoint("capped", "https://x/v1", "sk-555555555555", "m1", price_in=1_000_000,
                 price_out=0, daily_cap_usd=0.00001)
    f = Fleet([e], ledger=led, opener=Fake())
    led.record("capped", "m1", 1_000_000, 0, 0.00002, True)
    try:
        f.chat([{"role": "user", "content": "x"}])
        check(False, "超上限应当拒绝")
    except AllEndpointsFailed:
        check(True, "单 key 超日上限 → 不调用，报无可用端点")

    # 6. 舰队日上限
    led = fresh()
    f = Fleet([Endpoint("a", "https://x/v1", "sk-666666666666", "m1")], ledger=led,
              daily_cap_usd=0.000001, opener=Fake())
    led.record("a", "m1", 1_000_000, 0, 0.5, True)
    try:
        f.chat([{"role": "user", "content": "x"}])
        check(False, "舰队超上限应抛 BudgetExceeded")
    except BudgetExceeded:
        check(True, "舰队日上限 → BudgetExceeded")

    # 7. 空 key 跳过
    f = Fleet([Endpoint("nokey", "https://x/v1", "", "m1", priority=0),
               Endpoint("has", "https://x/v1", "sk-777777777777", "m1", priority=10)], opener=Fake())
    check([e.name for e in f.candidates()] == ["has"], "空 key 的端点被跳过")

    # 8. 400 不空转
    led = fresh()
    f = Fleet([Endpoint("badreq", "https://x/v1", "sk-888888888888", "m1")], ledger=led,
              opener=Fake({"*": [(400, '{"error":"bad model"}')]}))
    try:
        f.chat([{"role": "user", "content": "x"}])
        check(False, "400 应抛错")
    except _FetchError as ex:
        check(ex.kind == "bad-request", "400 直接抛给调用方，不换 key 空转")

    # 9. 5xx 降级但不封
    led = fresh()
    e5 = Endpoint("flaky", "https://x/v1", "sk-aaaa00001111", "m1", priority=0)
    t = Fake({"aaaa00001111": [(503, "upstream down")]})
    f = Fleet([e5, Endpoint("ok", "https://x/v1", "sk-bbbb00002222", "m1", priority=10)],
              ledger=led, opener=t)
    r = f.chat([{"role": "user", "content": "x"}])
    check(r.endpoint == "ok", "5xx → 换下一个端点")
    check(not e5.cooled_down(), "5xx 不封禁，只降优先级")
    check(e5.priority > 0, "5xx 后排到队尾")

    # 10. 健康检查
    t = Fake()
    f = Fleet([Endpoint("a", "https://x/v1", "sk-999999999999", "m1"), Endpoint("b", "https://x/v1", "", "m1")],
              opener=t)
    st = f.check()
    check(st[0]["ok"] and st[0]["detail"] == "0 个模型", "/models 健康检查")
    check(st[1]["ok"] is False and st[1]["kind"] == "missing" and "没有 key" in st[1]["detail"],
          "空 key 报告为不可用")

    # 10b. 体检要能分诊：额度尽 / 出口被挡 / 无效，处置各不相同
    t = Fake({
        "sk-aaaa0000q": [(402, "Insufficient Balance")],
        "sk-bbbb0000g": [(429, "Experiment web download traffic cap reached.")],
        "sk-cccc0000a": [(401, '{"error":{"message":"Invalid API key sk-cccc0000a1234567890"}}')],
    })
    f = Fleet([Endpoint("quota", "https://x/v1", "sk-aaaa0000q", "m1"),
               Endpoint("gate", "https://y/v1", "sk-bbbb0000g", "m1"),
               Endpoint("dead", "https://z/v1", "sk-cccc0000a", "m1")], opener=t)
    rows = {r["endpoint"]: r for r in f.check()}
    check(rows["quota"]["kind"] == "quota", "402 → 额度尽，不是 key 失效")
    check(rows["gate"]["kind"] == "local-gate", "平台限额文案 → local-gate")
    check(rows["dead"]["kind"] == "auth", "401 → key 失效")
    check("sk-cccc0000a1234567890" not in rows["dead"]["detail"], "报告里不泄露完整 key")

    # 10c. 体检不记账、不动冷却（只读观察）
    t = Fake({"*": [(429, "slow down")]})
    led2 = fresh()
    e = Endpoint("n", "https://x/v1", "sk-123412341234", "m1")
    f = Fleet([e], ledger=led2, opener=t)
    f.check()
    check(led2.calls_today() == 0, "体检不写账本")
    check(not e.cooled_down(), "体检不触发冷却")

    # 11. token 粗估
    from keyfleet.core import estimate_tokens
    a = estimate_tokens("hello world this is english text")
    b = estimate_tokens("这是一段中文文本用于测试")
    check(8 <= a <= 20, f"英文估 token 合理 ({a})")
    check(4 <= b <= 20, f"中文估 token 合理 ({b})")

    # 12. 配置文件 + env key
    d = tempfile.mkdtemp()
    os.environ["KF_TEST_KEY"] = "sk-envkey-000000"
    p = os.path.join(d, "fleet.json")
    with open(p, "w") as fh:
        json.dump({"env_prefix": "KF", "endpoints": [
            {"name": "fromenv", "base_url": "https://x/v1", "api_key_env": "KF_TEST_KEY", "model": "m1"}],
            "ledger": os.path.join(d, "l.db"), "daily_cap_usd": 2.0}, fh)
    f = Fleet.from_file(p, ledger_path=os.path.join(d, "l.db"))
    check(f.endpoints[0].api_key == "sk-envkey-000000", "从环境变量读 key")
    check(f.daily_cap_usd == 2.0, "读入舰队日上限")

    # 13. 冷启动全挂时不抛未捕获异常
    led = fresh()
    f = Fleet([Endpoint("a", "https://x/v1", "sk-cccc00001111", "m1")], ledger=led,
              opener=Fake({"*": [(500, "boom")]}))
    try:
        f.chat([{"role": "user", "content": "x"}])
        check(False, "全挂应抛 AllEndpointsFailed")
    except AllEndpointsFailed as ex:
        check("a=" in str(ex), "AllEndpointsFailed 带每次尝试细节")


    # 14. 平台出口限额（假 429）不能被算成 key 的问题
    check(classify(429, "Experiment web download traffic cap reached.") == "local-gate",
          "限额文案 → local-gate 而不是 quota")
    check(classify(429, '{"error":{"message":"Rate limit reached"}}') == "quota",
          "上游真 429 仍是 quota")
    check(is_local_gate(403, "blocked by egress policy") != "", "策略拒绝文案能识别")
    check(is_local_gate(429, "whatever", "Content-Type: text/plain") != "",
          "429+text/plain+无 request-id 视为中间层")
    check(is_local_gate(429, "whatever", "Content-Type: application/json\nX-Request-Id: abc") == "",
          "带 request-id 的 JSON 429 不误判")

    led = fresh()
    t = Fake({"*": [(429, "Experiment web download traffic cap reached.")]})
    e1 = Endpoint("k1", "https://x/v1", "sk-000000000001", "m1", priority=0)
    e2 = Endpoint("k2", "https://x/v1", "sk-000000000002", "m1", priority=10)
    f = Fleet([e1, e2], ledger=led, opener=t)
    try:
        f.chat([{"role": "user", "content": "x"}])
        check(False, "出口限额应抛 LocalGateBlocked")
    except LocalGateBlocked as ex:
        check("gate" in str(ex) or ex.reason, "LocalGateBlocked 带原因")
    check(len(t.calls) == 1, "出口限额后立刻停手，不再拿第二把 key 去撞同一扇门")
    check(e1.cooldown_until - time.time() < 3600, "没有把 key 停用 6 小时")
    check(e2.cooldown_until < time.time(), "备用 key 完全没被牵连")

    print(f"\n{len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
