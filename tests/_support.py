"""`agent/tests` 的公共夹具：临时被测项目、模块映射、ReviewConfig 与 fake LLM。

**为什么测试文件要自己往 sys.path 里塞路径**：本仓库的 editable 安装并不生效
（`import udma_test_review_agent` 在 venv 里直接 ModuleNotFoundError），
`scripts/*.py` 因此都以 `sys.path.insert(0, ".../src")` 自举；测试沿用同一约定。
各测试文件在 import 本模块前先插入 src 与本目录，故两种运行方式都成立：

    cd agent && .venv/bin/python -m unittest discover -s tests -v
    cd agent && .venv/bin/python -m unittest tests.test_findings_util -v

本模块只提供夹具，不含断言；**它不 import 被测模块**（由调用方插入路径后再 import），
以免 import 顺序反过来绑住调用方。
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Callable, Iterator

_SRC = Path(__file__).resolve().parents[1] / "src"
_TESTS = Path(__file__).resolve().parent


def bootstrap() -> None:
    """把 src 与本目录放进 sys.path[0..1]。各测试文件开头调用一次。"""
    for path in (str(_TESTS), str(_SRC)):
        if path not in os.sys.path:
            os.sys.path.insert(0, path)


def _quiet_console_and_route_file_log() -> None:
    """测试期把控制台降到 WARNING，并把日志文件挪到临时目录。

    `log.py::get_logger` 会把 DEBUG 全量写进 `agent/output/llm_test.log`——那是真实运行的
    心跳日志（约 5s 一条），测试顺手往里灌几百行噪声会让它失去参考价值。等级则纯粹是
    噪音问题：断言失败时才需要那几行上下文。

    两件事都要**对已存在的 logger 补做一遍**：`get_logger` 只在首次建 handler 时读
    `LOG_LEVEL` 与 `_LOG_FILE`，而测试文件的 import 顺序由调用方决定——被测模块常常
    先于本模块被导入，那时 handler 已经指向真实日志文件、等级也已经定成 INFO。
    只改模块级常量对它们无效（实测：控制台照样刷 INFO，真实日志照样被写脏）。
    """
    os.environ.setdefault("LOG_LEVEL", "WARNING")
    try:
        from udma_test_review_agent import log as agent_log
    except ImportError:  # 调用方还没把 src 放进 sys.path
        return
    # 尚未建 handler 的 logger 会读到这两个常量，故先改掉；原路径留着定位旧 handler。
    original, log_file = agent_log._LOG_FILE, Path(tempfile.mkdtemp(prefix="udma-test-logs-")) / "test.log"
    agent_log._LOG_DIR = log_file.parent
    agent_log._LOG_FILE = log_file
    agent_log.set_log_level("WARNING")  # 压掉包内 logger 的控制台输出
    _retarget_file_handlers(original, log_file)


def _retarget_file_handlers(original: Path, log_file: Path) -> None:
    """把已建的 FileHandler 从 `original` 换到 `log_file`，并压掉同 logger 的控制台。

    按**文件路径**而非 logger 名匹配：`scripts/run_review.py` 这类不在包内的入口也持有同一
    个日志文件的 handler，按 `udma_test_review_agent.` 前缀过滤会漏掉它们，真实日志照样被写。
    重建 handler 而非改 `stream`：`FileHandler` 的 `baseFilename`/`stream` 是内部状态，
    只改一半会让它继续往旧文件写或写坏。
    """
    names = [None, *logging.Logger.manager.loggerDict]  # None = root
    for name in names:
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if not isinstance(handler, logging.FileHandler):
                continue
            if Path(handler.baseFilename) != original:
                continue
            formatter = handler.formatter
            logger.removeHandler(handler)
            handler.close()
            replacement = logging.FileHandler(log_file, encoding="utf-8")
            replacement.setLevel(logging.DEBUG)  # 文件恒为 DEBUG，与生产一致
            replacement.setFormatter(formatter)
            logger.addHandler(replacement)
            for other in logger.handlers:  # 同一个 logger 的控制台
                if not isinstance(other, logging.FileHandler):
                    other.setLevel(logging.WARNING)


@contextlib.contextmanager
def env(**values: str | None) -> Iterator[None]:
    """临时设置环境变量；值为 None 表示删除。退出时恢复原值。"""
    saved = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TempProject:
    """临时被测项目：写源码文件、生成模块映射、产出 ReviewConfig。

    模块映射走 `MODULES_CONFIG` 环境变量指向临时 yaml——`load_modules_map` 不做缓存，
    故各测试之间互不污染。
    """

    def __init__(self, files: dict[str, str], mapping: dict[str, str] | None = None):
        self._tmp = tempfile.TemporaryDirectory(prefix="udma-test-")
        self.root = Path(self._tmp.name) / "project"
        self.root.mkdir(parents=True)
        for rel, text in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        if mapping is None:
            c_files = sorted(rel for rel in files if rel.endswith(".c"))
            mapping = {rel: f"M{index:02d}" for index, rel in enumerate(c_files, 1)}
        self.mapping = mapping
        self._config_dir = Path(self._tmp.name) / "config"
        self._config_dir.mkdir()
        self.modules_config = self._config_dir / "modules.yaml"
        lines = ["modules:"]
        for rel, module_id in mapping.items():
            lines.append(f"  - id: {module_id}")
            lines.append(f"    path: {rel}")
        self.modules_config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def scan(self, module_ids: dict[str, str] | None = None):
        from udma_test_review_agent.tools.executors import c_scanner

        return c_scanner.scan_project(str(self.root), module_ids or self.mapping)

    def config(self, **overrides: Any):
        from udma_test_review_agent.configuration import ReviewConfig

        with env(MODULES_CONFIG=str(self.modules_config)):
            return ReviewConfig(project_root=str(self.root), **overrides)

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
        return path

    def cleanup(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> TempProject:
        return self

    def __exit__(self, *exc: object) -> bool:
        self.cleanup()
        return False


def finding(**overrides: Any) -> dict[str, Any]:
    """构造一条满足 `schemas.Finding` 必填字段的条目（可覆盖任意字段）。"""
    data: dict[str, Any] = {
        "id": "f1",
        "category": "memory",
        "severity": "严重",
        "title": "测试条目",
        "description": "描述",
        "root_cause": "根因",
        "impact": "影响",
        "evidence": "证据",
        "suggestion": "修复",
        "file": "a.c",
        "line": 3,
        "call_chain": [],
        "defect_kind": "unbounded_write",
    }
    data.update(overrides)
    return data


_YAML_SEQ = itertools.count(1)


def write_models_yaml(
    directory: Path,
    *,
    active: str = "cheap",
    roles: dict[str, str] | None = None,
    temperature: float = 0.7,
) -> Path:
    """在 `directory` 下写一份最小 models.yaml，返回其路径。

    每次调用写**新路径**，因此不受 `load_models_config` 的 (mtime, size) 缓存影响
    ——同路径改内容在 mtime 粒度内可能命中旧缓存，测试里踩这个坑会极难排查。
    密钥走 `${TEST_API_KEY}` 引用，便于验证「字面量为空时回退环境变量」这条链，
    它正是 `--list-models` 曾误报「未配置」的地方。
    """
    lines = [f"active: {active}"]
    if roles:
        lines.append("roles:")
        lines += [f"  {role}: {target}" for role, target in roles.items()]
    else:
        lines.append("roles: {}")
    lines += [
        "defaults:",
        "  type: openai",
        f"  temperature: {temperature}",
        "providers:",
        "  p:",
        "    base_url: https://example.invalid/v1",
        "    api_key: ${TEST_API_KEY}",
        "models:",
        "  - name: cheap",
        "    provider: p",
        "    model: cheap-model",
        "  - name: strong",
        "    provider: p",
        "    model: strong-model",
    ]
    path = Path(directory) / f"models-{next(_YAML_SEQ)}.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def verdict(**overrides: Any) -> dict[str, Any]:
    """构造一条满足 `schemas.Verdict` 必填字段的结论（可覆盖任意字段）。"""
    data: dict[str, Any] = {
        "id": "f1",
        "conclusion": "confirmed",
        "severity": "严重",
        "confidence": 0.9,
        "rebuttal_opinion": "维持",
        "refutation_log": "逐点核对无异议",
        "evidence_note": "a.c:3",
        "contract_dependent": False,
    }
    data.update(overrides)
    return data


def result_item(item: dict[str, Any] | None = None, **verdict_overrides: Any) -> dict[str, Any]:
    """构造一条 `findings_verified.json` 里的 `{finding, verdict}` 记录。"""
    data = item or finding()
    return {"finding": data, "verdict": verdict(id=data["id"], **verdict_overrides)}


def arch_payload(module: str = "M01", **overrides: Any) -> dict[str, Any]:
    """构造一份满足 `schemas.ModuleArchitecture` 的架构产物。"""
    data: dict[str, Any] = {
        "module": module,
        "responsibility": "测试模块",
        "interfaces": [],
        "domains": [],
        "concurrency_contract": {"thread_model": "single_thread"},
        "checklist": [],
    }
    data.update(overrides)
    return data


def checklist_item(item: str = "核对边界", file: str = "a.c", line: int = 3, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {"category": "memory", "item": item, "file": file, "line": line}
    data.update(overrides)
    return data


def llm_by_schema(mapping: dict[Any, Any]) -> Callable[..., Any]:
    """按 Pydantic schema 分派的 LLM 桩件。

    检视路径一次调用链里会要两种产出：`RawFindings`（检视结果）与 `ReviewCheck`
    （内容审查）。按 schema 分派比按调用次序分派稳定得多——加一次重试、换个批次顺序，
    前者不用改，后者会把断言变成对调用序的隐式依赖。

    取值可以是单个值或列表；同一 schema 多次调用按列表逐次取用，用尽后重复最后一个。
    """
    counters: dict[Any, int] = {}

    def _fake(prompt: str, schema: Any, cfg: Any = None, *args: Any, **kwargs: Any) -> Any:
        if schema not in mapping:
            raise AssertionError(f"桩件未覆盖 schema={schema}（prompt 前 200 字：{prompt[:200]!r}）")
        value = mapping[schema]
        index = counters.get(schema, 0)
        counters[schema] = index + 1
        if isinstance(value, list):
            return value[min(index, len(value) - 1)]
        return value

    _fake.counters = counters  # type: ignore[attr-defined]
    return _fake


def raw_findings(findings: list[dict[str, Any]] | None = None, **overrides: Any):
    """构造 `RawFindings`（已过 schema 校验，故字段缺失会在此刻就报出来）。"""
    from udma_test_review_agent.schemas import RawFindings

    data: dict[str, Any] = {"findings": findings or []}
    data.update(overrides)
    return RawFindings.model_validate(data)


def llm_returning(*values: Any) -> Callable[..., Any]:
    """返回一个每次调用依次吐出 values 的桩件；用尽后重复最后一个。

    检视节点会按批次多次调用 LLM（小模块检视 + 内容审查，或大模块的逐文件批次），
    逐个返回值比 `side_effect=list` 更好用：次数超出预期时不会抛 StopIteration 掩盖真问题。
    """
    calls: list[Any] = []

    def _fake(*args: Any, **kwargs: Any) -> Any:
        index = min(len(calls), len(values) - 1)
        calls.append(args)
        return values[index]

    _fake.calls = calls  # type: ignore[attr-defined]
    return _fake


# 导入本模块即生效：各测试文件都先插好 sys.path 再 import 它，故此处 src 已在路径上。
_quiet_console_and_route_file_log()
