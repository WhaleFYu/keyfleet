"""keyfleet —— 多把 OpenAI 兼容 API key 的自动故障转移与花费上限。

零依赖（只用标准库）。设计出发点很朴素：手上有一堆"额度有限、随时可能被吊销、
随时可能 429"的 key 时，你想要的其实不是更聪明的重试，而是**一个账本 + 一条队列**。

    from keyfleet import Fleet
    fleet = Fleet.from_file("fleet.json")
    r = fleet.chat([{"role": "user", "content": "hi"}])
    print(r.text, r.endpoint.name, r.cost_usd)
"""
from .core import (
    Endpoint, Fleet, FleetError, AllEndpointsFailed, BudgetExceeded,
    Result, Ledger,
)

__version__ = "0.1.0"
__all__ = [
    "Endpoint", "Fleet", "FleetError", "AllEndpointsFailed", "BudgetExceeded",
    "Result", "Ledger", "__version__",
]
