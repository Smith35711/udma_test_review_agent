"""评审运行控制：暂停/继续/取消（协作式，节点边界与事件间隙生效）。"""

from __future__ import annotations

import threading


class ReviewCancelled(Exception):
    """评审被取消。"""


class RunControl:
    def __init__(self) -> None:
        self._paused = threading.Event()
        self._cancel = threading.Event()
        self._state = "idle"  # idle | running | paused | cancelled

    @property
    def state(self) -> str:
        return self._state

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def start(self) -> None:
        self._paused.clear()
        self._cancel.clear()
        self._state = "running"

    def pause(self) -> bool:
        if self._state != "running":
            return False
        self._state = "paused"
        self._paused.set()
        return True

    def resume(self) -> bool:
        if self._state != "paused":
            return False
        self._state = "running"
        self._paused.clear()
        return True

    def cancel(self) -> bool:
        if self._state not in ("running", "paused"):
            return False
        self._state = "cancelled"
        self._cancel.set()
        self._paused.set()  # 唤醒暂停等待
        return True

    def reset_state(self, state: str) -> None:
        self._state = state

    def checkpoint(self) -> None:
        """节点开始前调用：等待暂停；检测取消。"""
        if self._cancel.is_set():
            raise ReviewCancelled("评审已被取消")
        while self._paused.is_set():
            if self._paused.wait(timeout=0.5):
                if self._cancel.is_set():
                    raise ReviewCancelled("评审已被取消")


run_control = RunControl()
