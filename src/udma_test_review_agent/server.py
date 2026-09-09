"""评审智能体 Web 服务：拓扑/节点状态/LLM 流式/日志，SSE 推送。"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .configuration import ReviewConfig
from .control import run_control
from .graph import graph
from .log import resolve_level
from .model_config import load_models_config
from .versioning import prepare_run

_WEB_DIR = Path(__file__).resolve().parents[2] / "web"

MERMAID = """flowchart TD
    START([START]) --> scan
    subgraph arch[架构分析阶段]
        scan["S0 仓库扫描 · 工具"]
        global_arch["S1 全局架构 · LLM"]
        module_archs["S2 模块架构 · LLM"]
        critical_paths["S3 关键路径 · LLM"]
        merge["S4 汇总校验 · 工具"]
    end
    subgraph loop[模块评审循环]
        size_analysis["尺寸分析 · 工具"]
        review_module["检视 · LLM"]
        rebuttal_module["反驳 · LLM"]
        review_check["反驳审查 · 工具"]
        module_report["模块报告 · LLM"]
        advance["推进 · 工具"]
    end
    scan --> global_arch --> module_archs --> critical_paths --> merge
    merge --> size_analysis --> review_module --> rebuttal_module --> review_check --> module_report --> advance
    advance -->|下一模块| size_analysis
    advance -->|全部完成| report["总报告 · 工具"]
    report --> END([END])
"""

LLM_NODES = ["global_arch", "module_archs", "critical_paths", "review_module", "rebuttal_module", "module_report"]
TOOL_NODES = ["scan", "merge", "size_analysis", "review_check", "advance", "report"]
ALL_NODES = TOOL_NODES + LLM_NODES


class _Bus:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: deque[tuple[int, dict[str, Any]]] = deque(maxlen=3000)
        self.seq = 0

    def publish(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.seq += 1
            self.events.append((self.seq, event))

    def after(self, seq: int) -> list[tuple[int, dict[str, Any]]]:
        with self.lock:
            return [item for item in self.events if item[0] > seq]

    def replay_tail(self, count: int) -> list[tuple[int, dict[str, Any]]]:
        with self.lock:
            return list(self.events)[-count:]


bus = _Bus()
run_state: dict[str, str] = {"status": "idle", "message": ""}
run_lock = threading.Lock()
last_payload: dict[str, Any] | None = None
worker_done = threading.Event()
worker_done.set()


class _BusHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            bus.publish({
                "type": "log",
                "level": record.levelname,
                "msg": f"{time.strftime('%H:%M:%S', time.localtime(record.created))} {record.getMessage()}",
            })
        except Exception:  # noqa: BLE001 - 日志总线异常不影响主流程
            pass


def _attach_log_bus() -> None:
    target = logging.getLogger("udma_test_review_agent")
    if not any(isinstance(h, _BusHandler) for h in target.handlers):
        handler = _BusHandler()
        handler.setLevel(resolve_level(os.getenv("LOG_LEVEL")))
        target.addHandler(handler)


_attach_log_bus()

app = FastAPI(title="udma-test-review-agent")


class RunRequest(BaseModel):
    project: str
    modules: str = "all"
    model: str = ""
    small_line_threshold: int = 2000
    clean: bool = False


def _execute(cfg_dict: dict[str, Any]) -> None:
    worker_done.clear()
    try:
        bus.publish({"type": "run", "status": "running"})
        for chunk in graph.stream({"config": cfg_dict}, stream_mode="custom"):
            if isinstance(chunk, dict) and chunk.get("type") in ("node", "llm"):
                bus.publish(chunk)
            if run_control.cancelled:
                break
        if run_control.cancelled:
            run_state.update(status="idle", message="已取消")
            bus.publish({"type": "run", "status": "cancelled"})
        else:
            run_control.reset_state("done")
            run_state.update(status="idle", message="评审完成")
            bus.publish({"type": "run", "status": "done"})
    except Exception as exc:  # noqa: BLE001 - 失败信息推送到前端
        run_control.reset_state("error")
        run_state.update(status="idle", message=str(exc)[:300])
        bus.publish({"type": "run", "status": "error", "message": str(exc)[:500]})
    finally:
        worker_done.set()


@app.post("/api/run")
def start_run(req: RunRequest) -> dict[str, str]:
    global last_payload
    with run_lock:
        if run_state["status"] in ("running", "paused"):
            raise HTTPException(status_code=409, detail="已有评审任务在运行/暂停中")
        try:
            cfg = ReviewConfig(
                project_root=req.project,
                modules=req.modules,
                model_profile=req.model,
                small_module_line_threshold=req.small_line_threshold,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        prepare_run(cfg.output_root, clean=req.clean)
        last_payload = req.model_dump()
        run_state.update(status="running", message="")
        run_control.start()
        threading.Thread(target=_execute, args=(asdict(cfg),), daemon=True).start()
    return {"status": "started"}


def _await_worker(timeout: float = 30.0) -> None:
    worker_done.wait(timeout=timeout)


@app.post("/api/pause")
def pause() -> dict[str, str]:
    if not run_control.pause():
        raise HTTPException(status_code=409, detail="当前状态不可暂停")
    run_state.update(status="paused", message="已暂停")
    bus.publish({"type": "run", "status": "paused"})
    return {"status": "paused"}


@app.post("/api/resume")
def resume() -> dict[str, str]:
    if not run_control.resume():
        raise HTTPException(status_code=409, detail="当前状态不可继续")
    run_state.update(status="running", message="继续运行")
    bus.publish({"type": "run", "status": "running"})
    return {"status": "running"}


@app.post("/api/cancel")
def cancel() -> dict[str, str]:
    if not run_control.cancel():
        raise HTTPException(status_code=409, detail="当前无运行任务")
    run_state.update(status="idle", message="已请求取消")
    return {"status": "cancelling"}


@app.post("/api/restart")
def restart() -> dict[str, str]:
    global last_payload
    with run_lock:
        if last_payload is None:
            raise HTTPException(status_code=400, detail="尚无已运行的参数，请先启动一次")
        if run_state["status"] == "running":
            run_control.cancel()
            _await_worker()
        payload = dict(last_payload)
        cfg = ReviewConfig(
            project_root=payload["project"],
            modules=payload.get("modules", "all"),
            model_profile=payload.get("model", ""),
            small_module_line_threshold=payload.get("small_line_threshold", 2000),
        )
        prepare_run(cfg.output_root, clean=True)
        run_state.update(status="running", message="")
        run_control.start()
        threading.Thread(target=_execute, args=(asdict(cfg),), daemon=True).start()
    return {"status": "started"}


@app.get("/api/models")
def get_models() -> dict[str, Any]:
    try:
        cfg = load_models_config()
    except ValueError as exc:
        return {"active": "", "models": [], "error": str(exc)}
    return {"active": cfg.active, "models": [m.name for m in cfg.models]}


@app.get("/api/graph")
def get_graph() -> dict[str, Any]:
    return {
        "mermaid": MERMAID,
        "nodes": ALL_NODES,
        "llm_nodes": LLM_NODES,
        "tool_nodes": TOOL_NODES,
    }


@app.get("/api/status")
def get_status() -> dict[str, str]:
    return run_state


@app.get("/api/events")
def events() -> StreamingResponse:
    def generator():
        history = bus.replay_tail(200)
        seq = history[-1][0] if history else 0
        for _, event in history:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        while True:
            for item_seq, event in bus.after(seq):
                seq = item_seq
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            time.sleep(0.3)

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_WEB_DIR / "index.html")
