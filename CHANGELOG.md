# Changelog

## 0.2.0 — 2026-11-25

- `keyfleet check` 重写：并发探活（默认 8 路）、读响应体做分诊、输出合计与处置建议。
  - 新增 `kind` 字段：`ok` / `auth` / `quota` / `local-gate` / `server` / `bad-request` / `unreachable` / `missing`。
  - 报文裁剪 + 密钥抹除：报告可直接粘进 issue。
  - 只读：不记账、不动冷却表。
- 新增 `--json` / `--workers`；CLI 退出码沿用 0/1（全通/有问题）。
- 发行名改为 `keyfleet-llm`（PyPI 上 `keyfleet` 已被另一个硬件密钥项目占用），导入名仍是 `keyfleet`。
- 测试 28 → 43 项。

## 0.1.x

- 多 key 故障转移、日花费上限、sqlite 账本、五类错误分流（含 `local-gate`）。
