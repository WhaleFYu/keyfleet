"""核心实现：账本（Ledger）+ 舰队（Fleet）。

设计取舍（写清楚，免得后来者以为是漏了）：
1. **不做语义级重试**：429 就是 429，换个 key 比退避等待更划算。
2. **账本落盘**：花费按"endpoint + UTC 日"累计在本地 sqlite 里，
   进程重启不丢；上限是硬闸门，宁可不调用也不越界。
3. **冷却表**：鉴权失败 → 停用 6h；配额/限流 → 退避 5min 起指数增长，封顶 1h；
   网络/5xx → 只降到队尾，不封。
4. **不猜价格**：价格由调用方在配置里写死（每 100 万 token 的输入/输出单价）。
   猜价格比不记账更危险。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Iterable


class FleetError(Exception):
    """基类。"""


class AllEndpointsFailed(FleetError):
    def __init__(self, attempts: list[dict]):
        self.attempts = attempts
        detail = "; ".join(f"{a['endpoint']}={a['error']}" for a in attempts) or "no endpoint available"
        super().__init__(f"all endpoints failed: {detail}")


class BudgetExceeded(FleetError):
    pass


# ---------------------------------------------------------------- endpoint

@dataclass
class Endpoint:
    """一个 OpenAI 兼容端点。api_key 永远不会出现在 repr/日志里。"""

    name: str
    base_url: str
    api_key: str
    model: str = ""
    price_in: float = 0.0      # USD / 1M input tokens
    price_out: float = 0.0     # USD / 1M output tokens
    daily_cap_usd: float | None = None
    priority: int = 100        # 越小越先用
    tags: list[str] = field(default_factory=list)
    timeout: float = 60.0
    extra_headers: dict[str, str] = field(default_factory=dict)

    # 运行时状态（不落配置）
    cooldown_until: float = 0.0
    failures: int = 0
    _rpm_limit: int | None = None

    def __repr__(self) -> str:  # 绝不泄露 key
        return (f"Endpoint(name={self.name!r}, base_url={self.base_url!r}, "
                f"model={self.model!r}, key={self.key_hint()}, "
                f"daily_cap_usd={self.daily_cap_usd})")

    def key_hint(self) -> str:
        k = self.api_key or ""
        return f"{k[:4]}…{k[-4:]}" if len(k) >= 12 else "…"

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    @property
    def models_url(self) -> str:
        return self.base_url.rstrip("/") + "/models"

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (prompt_tokens * self.price_in + completion_tokens * self.price_out) / 1_000_000.0

    def cooled_down(self, now: float | None = None) -> bool:
        return (now or time.time()) < self.cooldown_until

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("api_key", None)
        d["key_hint"] = self.key_hint()
        d["cooldown_until"] = self.cooldown_until
        d["failures"] = self.failures
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Endpoint":
        known = {f for f in cls.__dataclass_fields__ if not f.startswith("_")}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------- ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  ts REAL NOT NULL,
  day TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  model TEXT,
  prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  cost_usd REAL DEFAULT 0,
  ok INTEGER NOT NULL,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_day ON calls(day, endpoint);
"""


def utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%d")


class Ledger:
    """本地花费账本（sqlite）。文件权限 0600。"""

    def __init__(self, path: str = "~/.keyfleet/ledger.db"):
        self.path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        new = not os.path.exists(self.path)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)
        self.db.commit()
        if new:
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    def record(self, endpoint: str, model: str, prompt_tokens: int, completion_tokens: int,
               cost: float, ok: bool, error: str | None = None, ts: float | None = None) -> None:
        ts = ts or time.time()
        self.db.execute(
            "INSERT INTO calls (ts,day,endpoint,model,prompt_tokens,completion_tokens,cost_usd,ok,error)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, utc_day(ts), endpoint, model, int(prompt_tokens), int(completion_tokens),
             float(cost), 1 if ok else 0, error),
        )
        self.db.commit()

    def spend(self, endpoint: str | None = None, day: str | None = None) -> float:
        q = "SELECT COALESCE(SUM(cost_usd),0) FROM calls WHERE day = ?"
        args: list[Any] = [day or utc_day()]
        if endpoint:
            q += " AND endpoint = ?"
            args.append(endpoint)
        return float(self.db.execute(q, args).fetchone()[0] or 0.0)

    def calls_today(self, endpoint: str | None = None) -> int:
        q = "SELECT COUNT(*) FROM calls WHERE day = ?"
        args: list[Any] = [utc_day()]
        if endpoint:
            q += " AND endpoint = ?"
            args.append(endpoint)
        return int(self.db.execute(q, args).fetchone()[0] or 0)

    def report(self, days: int = 7) -> list[dict]:
        rows = self.db.execute(
            "SELECT day, endpoint, COUNT(*), SUM(ok), SUM(cost_usd), SUM(prompt_tokens),"
            " SUM(completion_tokens) FROM calls GROUP BY day, endpoint"
            " ORDER BY day DESC, SUM(cost_usd) DESC LIMIT ?", (days * 20,)
        ).fetchall()
        return [{"day": r[0], "endpoint": r[1], "calls": r[2], "ok": r[3],
                 "cost_usd": round(r[4] or 0.0, 6), "prompt_tokens": r[5] or 0,
                 "completion_tokens": r[6] or 0} for r in rows]

    def close(self) -> None:
        self.db.close()


# ---------------------------------------------------------------- result

@dataclass
class Result:
    text: str
    endpoint: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    raw: dict
    attempts: int = 1


# ---------------------------------------------------------------- helpers

class _FetchError(Exception):
    def __init__(self, kind: str, status: int | None, message: str):
        super().__init__(message)
        self.kind = kind          # auth | quota | server | network | bad-request | unknown
        self.status = status
        self.message = message


def classify(status: int, body: str) -> str:
    """把 HTTP 状态码 + 响应体归到 4 类处置逻辑里。"""
    low = (body or "").lower()
    if status in (401, 403):
        return "auth"
    if status == 402 or "insufficient" in low or "quota" in low or "balance" in low:
        return "quota"
    if status == 429 or "rate limit" in low:
        return "quota"
    if status and status >= 500:
        return "server"
    if status in (400, 404, 422):
        return "bad-request"
    return "unknown"


def estimate_tokens(text: str) -> int:
    """粗估 token 数：CJK 按 1 字 ≈ 0.7 token，其余按 4 字符 ≈ 1 token。

    它只用于**花前预检**（防止一次爆掉日上限），真实计量一律以响应的 usage 为准。
    """
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text or ""))
    rest = len(text or "") - cjk
    return int(cjk * 0.7 + rest / 4) + 8


_BACKOFF = {  # kind -> (首次退避秒, 上限秒)
    "auth": (6 * 3600, 6 * 3600),
    "quota": (300, 3600),
}


class Fleet:
    """按 priority 顺序挑一把能用、且没超当日花费上限的 key。"""

    def __init__(self, endpoints: Iterable[Endpoint], ledger: Ledger | None = None,
                 daily_cap_usd: float | None = None, opener=None):
        self.endpoints: list[Endpoint] = list(endpoints)
        if not self.endpoints:
            raise ValueError("Fleet 至少需要一个 endpoint")
        self.ledger = ledger or Ledger()
        self.daily_cap_usd = daily_cap_usd      # 全舰队日上限（可选）
        self._opener = opener or urllib.request.urlopen
        self.last_attempts: list[dict] = []

    # -- 构造 ------------------------------------------------------
    @classmethod
    def from_file(cls, path: str, ledger_path: str | None = None) -> "Fleet":
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        env_prefix = cfg.get("env_prefix", "")
        eps = []
        for i, raw in enumerate(cfg["endpoints"]):
            raw = dict(raw)
            # 支持 {"api_key_env": "DEEPSEEK_KEY"}，避免把 key 写进配置文件
            if "api_key_env" in raw:
                raw["api_key"] = os.environ.get(raw["api_key_env"], "")
            ep = Endpoint.from_dict(raw)
            ep.priority = ep.priority if "priority" in raw else i * 10
            eps.append(ep)
        ledger = Ledger(ledger_path or cfg.get("ledger", "~/.keyfleet/ledger.db"))
        return cls(eps, ledger=ledger, daily_cap_usd=cfg.get("daily_cap_usd"))

    def add(self, ep: Endpoint) -> "Fleet":
        self.endpoints.append(ep)
        return self

    # -- 选择 ------------------------------------------------------
    def _remaining(self, ep: Endpoint) -> float | None:
        if ep.daily_cap_usd is None:
            return None
        return ep.daily_cap_usd - self.ledger.spend(ep.name)

    def candidates(self, need_usd: float = 0.0, tags: Iterable[str] | None = None) -> list[Endpoint]:
        now = time.time()
        tagset = set(tags or ())
        out = []
        for ep in self.endpoints:
            if not ep.api_key:
                continue
            if ep.cooled_down(now):
                continue
            if tagset and not (tagset & set(ep.tags)):
                continue
            rem = self._remaining(ep)
            if rem is not None and rem <= max(need_usd, 0.0):
                continue
            out.append(ep)
        out.sort(key=lambda e: (e.priority, e.failures))
        return out

    def status(self) -> dict:
        now = time.time()
        eps = []
        for ep in sorted(self.endpoints, key=lambda e: e.priority):
            rem = self._remaining(ep)
            eps.append({**ep.to_public(),
                        "spent_today_usd": round(self.ledger.spend(ep.name), 6),
                        "remaining_today_usd": None if rem is None else round(rem, 6),
                        "cooling_down_sec": max(0, int(ep.cooldown_until - now)),
                        "usable": bool(ep.api_key) and not ep.cooled_down(now)})
        return {"endpoints": eps, "fleet_spent_today_usd": round(self.ledger.spend(), 6),
                "daily_cap_usd": self.daily_cap_usd}

    # -- 惩罚 ------------------------------------------------------
    def _penalize(self, ep: Endpoint, kind: str) -> None:
        ep.failures += 1
        if kind in _BACKOFF:
            base, cap = _BACKOFF[kind]
            secs = min(cap, base * (2 ** max(0, ep.failures - 1)))
            ep.cooldown_until = time.time() + secs
        elif kind == "server":
            ep.priority += 5          # 只降级，不封
        # network / bad-request: 不动它

    def _reward(self, ep: Endpoint) -> None:
        if ep.failures:
            ep.failures = max(0, ep.failures - 1)

    # -- HTTP ------------------------------------------------------
    def _post(self, ep: Endpoint, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ep.api_key}",
            "User-Agent": "keyfleet/0.1 (+https://github.com/WhaleFYu)",
        }
        headers.update(ep.extra_headers or {})
        req = urllib.request.Request(ep.chat_url, data=data, headers=headers, method="POST")
        try:
            with self._opener(req, timeout=ep.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")[:600]
            except Exception:
                pass
            raise _FetchError(classify(e.code, body), e.code, f"HTTP {e.code}: {body[:200]}")
        except urllib.error.URLError as e:
            raise _FetchError("network", None, f"network: {e.reason}")
        except (TimeoutError, OSError) as e:
            raise _FetchError("network", None, f"network: {type(e).__name__}")

    # -- 主入口 ----------------------------------------------------
    def chat(self, messages: list[dict], model: str | None = None, *, tags=None,
             temperature: float | None = None, max_tokens: int | None = None,
             extra: dict | None = None, max_attempts: int | None = None) -> Result:
        est_in = sum(estimate_tokens(m.get("content", "")) for m in messages)
        est_out = max_tokens or 512
        # 用最贵的那把 key 估一个上界，避免"便宜的先跑、贵的后爆"
        worst = max((e.price_in for e in self.endpoints), default=0.0)
        worst_out = max((e.price_out for e in self.endpoints), default=0.0)
        need = (est_in * worst + est_out * worst_out) / 1_000_000.0

        if self.daily_cap_usd is not None and self.ledger.spend() + need > self.daily_cap_usd:
            raise BudgetExceeded(
                f"fleet daily cap {self.daily_cap_usd} USD reached "
                f"(spent {self.ledger.spend():.6f}, need ~{need:.6f})")

        pool = self.candidates(need_usd=need, tags=tags)
        if not pool:
            raise AllEndpointsFailed([{"endpoint": "(none)", "error": "no usable endpoint (cooldown/empty key/cap)"}])

        attempts = []
        for ep in pool[: max_attempts or len(pool)]:
            mdl = model or ep.model
            payload: dict[str, Any] = {"messages": messages}
            if mdl:
                payload["model"] = mdl
            if temperature is not None:
                payload["temperature"] = temperature
            if max_tokens is not None:
                payload["max_tokens"] = max_tokens
            payload.update(extra or {})
            try:
                data = self._post(ep, payload)
            except _FetchError as e:
                attempts.append({"endpoint": ep.name, "error": f"{e.kind}: {e.message}"})
                self.ledger.record(ep.name, mdl or "", 0, 0, 0.0, False, f"{e.kind}: {e.message}")
                self._penalize(ep, e.kind)
                if e.kind == "bad-request":
                    # 请求本身有问题，换 key 无用；直接把上游错误抛给调用方
                    self.last_attempts = attempts
                    raise
                continue

            usage = data.get("usage") or {}
            pt = int(usage.get("prompt_tokens") or 0)
            ct = int(usage.get("completion_tokens") or 0)
            cost = ep.cost(pt, ct)
            text = _extract_text(data)
            if not text:
                attempts.append({"endpoint": ep.name, "error": "empty completion"})
                self.ledger.record(ep.name, mdl or "", pt, ct, cost, False, "empty completion")
                self._penalize(ep, "server")
                continue
            self.ledger.record(ep.name, mdl or "", pt, ct, cost, True, None)
            self._reward(ep)
            self.last_attempts = attempts + [{"endpoint": ep.name, "error": None}]
            return Result(text=text, endpoint=ep.name, model=data.get("model") or mdl or "",
                          prompt_tokens=pt, completion_tokens=ct, cost_usd=cost,
                          raw=data, attempts=len(attempts) + 1)

        self.last_attempts = attempts
        raise AllEndpointsFailed(attempts)

    # -- 健康检查 --------------------------------------------------
    def check(self, timeout: float = 12.0) -> list[dict]:
        """打 /models 验证每把 key 的活性；不改冷却表，只报告。"""
        out = []
        for ep in self.endpoints:
            item = {"endpoint": ep.name, "key": ep.key_hint(), "ok": False, "detail": ""}
            if not ep.api_key:
                item["detail"] = "no key"
                out.append(item)
                continue
            req = urllib.request.Request(
                ep.models_url, headers={"Authorization": f"Bearer {ep.api_key}",
                                        "User-Agent": "keyfleet/0.1"})
            t = time.time()
            try:
                with self._opener(req, timeout=timeout) as resp:
                    body = resp.read().decode("utf-8", "replace")
                item["ok"] = True
                try:
                    models = json.loads(body).get("data") or []
                    item["detail"] = f"{len(models)} models"
                except Exception:
                    item["detail"] = "200"
            except urllib.error.HTTPError as e:
                item["detail"] = f"HTTP {e.code} ({classify(e.code, '')})"
            except Exception as e:
                item["detail"] = f"{type(e).__name__}"
            item["ms"] = int((time.time() - t) * 1000)
            out.append(item)
        return out

    def close(self) -> None:
        self.ledger.close()


def _extract_text(data: dict) -> str:
    try:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        text = msg.get("content")
        if isinstance(text, list):        # 部分兼容层返回分段内容
            text = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in text)
        if text:
            return text
        return choice.get("text") or ""
    except Exception:
        return ""
