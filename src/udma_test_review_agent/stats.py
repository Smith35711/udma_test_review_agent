"""运行期过程统计：token 用量、阶段耗时、条目漏斗。

设计文档 P6 要求「过滤率、token 用量、阶段耗时纳入报告，作为评审结论可信度依据」。
这三样此前分别是：一行 INFO 日志（跑完即散）、各节点自己算的局部耗时（格式各异、
无法汇总）、以及**从未被计算过**。

本模块用一个进程级、线程安全的累加器把它们集中起来，图跑完后由报告节点与 CLI 取出。
**它是旁路观测，不参与任何判定**：所有记账方法都不抛异常、取值一律给默认值——
统计本身出问题绝不能影响评审结论。

为什么用进程级单例而不是显式传递：节点是纯函数，由 LangGraph 调度，模块循环内还会
经 `ThreadPoolExecutor` 并发执行；把统计对象一路透传会污染每一个节点签名，而它没有
任何「按调用方区分」的语义。代价是测试之间需要 `reset_stats()`，可接受。
"""

from __future__ import annotations

import functools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

from .log import get_logger

logger = get_logger(__name__)

_T = TypeVar("_T")

STAGE_ORDER = (
    "scan",
    "tool_evidence",
    "global_arch",
    "module_archs",
    "critical_paths",
    "merge",
    "size_analysis",
    "review_module",
    "rebuttal_module",
    "review_check",
    "module_report",
    "advance",
    "report",
)
"""阶段的展示顺序，与 `graph.py` 的拓扑一致。表里出现未知阶段时排在末尾。"""


@dataclass
class LlmCall:
    """一次 LLM 调用的过程数据。失败调用也记账——重试恰恰是最不该漏记的用量。"""

    role: str = ""
    spec: str = ""
    """模型条目的 name（配置里的标识），路由是否生效看它与 `model` 的对应关系。"""
    model: str = ""
    """实际请求的模型 id。"""
    schema: str = ""
    ok: bool = True
    elapsed: float = 0.0
    prompt_chars: int = 0
    reasoning_chars: int = 0
    content_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    has_usage: bool = False
    """供应商是否返回了 usage。为假时 token 字段**不代表零消耗**，只代表未采集到——
    报告必须把这件事说清楚，否则「0 token」会被读成「没花钱」。"""
    error: str = ""


@dataclass
class _Stage:
    runs: int = 0
    elapsed: float = 0.0
    slowest: float = 0.0


class RunStats:
    """线程安全的运行期累加器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[LlmCall] = []
        self._stages: dict[str, _Stage] = {}
        self._started = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self._calls = []
            self._stages = {}
            self._started = time.monotonic()

    def record_llm(self, call: LlmCall) -> None:
        with self._lock:
            self._calls.append(call)

    def record_stage(self, name: str, elapsed: float) -> None:
        with self._lock:
            stage = self._stages.setdefault(name, _Stage())
            stage.runs += 1
            stage.elapsed += elapsed
            stage.slowest = max(stage.slowest, elapsed)

    def calls(self) -> list[LlmCall]:
        with self._lock:
            return list(self._calls)

    def stage_rows(self) -> list[dict[str, Any]]:
        """各阶段耗时。模块循环内的阶段会被执行多次，故同时给出次数与单次最慢值——
        只报总和会让「3 个模块共 90 秒」与「1 个模块 90 秒」看起来一样。"""
        with self._lock:
            stages = {name: _Stage(s.runs, s.elapsed, s.slowest) for name, s in self._stages.items()}
        rows = [
            {"stage": name, "runs": s.runs, "elapsed": round(s.elapsed, 3), "slowest": round(s.slowest, 3)}
            for name, s in stages.items()
        ]
        index = {name: i for i, name in enumerate(STAGE_ORDER)}
        rows.sort(key=lambda row: (index.get(row["stage"], len(STAGE_ORDER)), row["stage"]))
        return rows

    def snapshot(self) -> dict[str, Any]:
        """汇总为可 JSON 序列化的过程指标。"""
        calls = self.calls()
        with self._lock:
            started = self._started
        by_role: dict[str, dict[str, Any]] = {}
        for call in calls:
            key = call.role or "(默认)"
            slot = by_role.setdefault(
                key,
                {
                    "role": key,
                    "spec": call.spec,
                    "model": call.model,
                    "calls": 0,
                    "failed": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "usage_missing": 0,
                    "elapsed": 0.0,
                },
            )
            slot["calls"] += 1
            if not call.ok:
                slot["failed"] += 1
            slot["prompt_tokens"] += call.prompt_tokens
            slot["completion_tokens"] += call.completion_tokens
            slot["total_tokens"] += call.total_tokens
            if not call.has_usage:
                slot["usage_missing"] += 1
            slot["elapsed"] += call.elapsed
            # 同一角色可能因重试落到不同模型（换了 profile），此处按最后一次记录展示
            slot["model"] = call.model or slot["model"]
            slot["spec"] = call.spec or slot["spec"]
        role_rows = sorted(by_role.values(), key=lambda row: row["role"])
        for row in role_rows:
            row["elapsed"] = round(row["elapsed"], 3)
        return {
            "wall_clock": round(time.monotonic() - started, 3),
            "llm": {
                "calls": len(calls),
                "failed": sum(1 for c in calls if not c.ok),
                "usage_missing": sum(1 for c in calls if not c.has_usage),
                "prompt_tokens": sum(c.prompt_tokens for c in calls),
                "completion_tokens": sum(c.completion_tokens for c in calls),
                "total_tokens": sum(c.total_tokens for c in calls),
                "elapsed": round(sum(c.elapsed for c in calls), 3),
                "by_role": role_rows,
            },
            "stages": self.stage_rows(),
        }


STATS = RunStats()
"""进程级单例。CLI 启动与测试用例开头应调用 `reset_stats()`。"""


def get_stats() -> RunStats:
    return STATS


def reset_stats() -> None:
    STATS.reset()


def timed_stage(name: str, fn: Callable[..., _T]) -> Callable[..., _T]:
    """包装图节点函数，把该阶段的墙钟耗时记进统计。

    在图组装处统一包装，而不是让每个节点各自计时：节点自己计时的结果格式各异、
    覆盖不全（模块循环内的节点只在部分分支上有日志），无法汇总成一张表。
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> _T:
        started = time.monotonic()
        try:
            return fn(*args, **kwargs)
        finally:
            STATS.record_stage(name, time.monotonic() - started)

    return wrapper
