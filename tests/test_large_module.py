"""大模块分支的离线测试：假客户端驱动，零额度。

覆盖：符号表与调用图解析、分解方案的归属修补与行覆盖、循环调用组被拆散时退回重做、
子块并发检视的结果合并与失败容忍、探测分析的两轮取证与收敛。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent2 import csymbols, llm
from agent2.artifacts import read_json, read_text
from agent2.configuration import RunConfig
from agent2.graph import build_large_review_graph
from agent2.log import set_log_level
from agent2.nodes.call_graph_tool import cyclic_groups

set_log_level("WARNING")

# 两个函数相互递归，另有一个独立函数：分解方案必须把这两个放在同一块
SOURCE_C = """#include "mod.h"

#include <stdlib.h>

static int g_total = 0;

int step_a(int n) {
    if (n <= 0) {
        return 0;
    }
    return step_b(n - 1);
}

int step_b(int n) {
    if (n <= 0) {
        return 1;
    }
    return step_a(n - 1);
}

int helper(int x) {
    char *p = malloc(x);
    p[0] = '\\0';
    return g_total + x;
}
"""

HEADER = "#ifndef MOD_H\n#define MOD_H\n\nint step_a(int n);\nint step_b(int n);\nint helper(int x);\n\n#endif\n"


def _reply(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False)


class _Client:
    """按提示词里**注入的 schema 标题**分派回复。

    不能按调用顺序吐回复：子块是并发跑的，完成顺序不定，按顺序取必然随机失败。
    """

    def __init__(self, architecture: dict, review: dict, rebuttals: list[dict]):
        self.architecture = architecture
        self.review = review
        self.rebuttals = list(rebuttals)
        self.prompts: list[str] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        self.prompts.append(prompt)
        if '"title": "ArchitecturePlan"' in prompt:
            text = _reply(self.architecture)
        elif '"title": "ReviewResult"' in prompt:
            text = _reply(self.review)
        elif '"title": "ProbeRebuttalResult"' in prompt:
            # 取证之后的第二轮要换一份回复：用证据段是否为空来区分
            index = 1 if "轮取证结果" in prompt else 0
            text = _reply(self.rebuttals[min(index, len(self.rebuttals) - 1)])
        else:
            text = "{}"
        return _Stream(text)


class _Stream:
    def __init__(self, text: str):
        self._text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        delta = SimpleNamespace(content=self._text, reasoning_content=None)
        yield SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=None)
        yield SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=1, completion_tokens=1, total_tokens=2,
                completion_tokens_details=None, prompt_tokens_details=None,
            ),
        )


def _write_project(root: Path) -> Path:
    project = root / "project"
    project.mkdir()
    (project / "mod.c").write_text(SOURCE_C, encoding="utf-8")
    (project / "mod.h").write_text(HEADER, encoding="utf-8")
    return project


class SymbolTest(unittest.TestCase):
    def test_parses_functions_globals_and_cycles(self) -> None:
        with tempfile.TemporaryDirectory(prefix="agent2-large-") as tmp:
            project = _write_project(Path(tmp))
            functions, globals_ = csymbols.read_and_parse([project / "mod.c"], project)
            names = {f["name"] for f in functions}
            self.assertEqual(names, {"step_a", "step_b", "helper"})
            self.assertEqual([g["name"] for g in globals_], ["g_total"])

            edges = []
            for function in functions:
                text = (project / "mod.c").read_text(encoding="utf-8")
                for call in csymbols.find_calls(text, function, names):
                    edges.append({"caller": function["name"], "callee": call["callee"]})
            self.assertEqual(cyclic_groups(sorted(names), edges), [["step_a", "step_b"]])


class DependencyTest(unittest.TestCase):
    """背景信息：只收项目内部依赖，标准库与系统调用一概不要。"""

    def test_only_internal_dependencies_are_reported(self) -> None:
        from agent2.nodes.dependencies_tool import dependencies_tool

        with tempfile.TemporaryDirectory(prefix="agent2-dep-") as tmp:
            root = Path(tmp)
            (root / "base.h").write_text(
                "#ifndef BASE_H\n#define BASE_H\n\n"
                "#define BASE_LIMIT 16\n#define BASE_SQUARE(x) ((x) * (x))\n"
                "typedef struct { int id; char name[BASE_LIMIT]; } base_item_t;\n"
                "int base_open(const char *path);\n\n#endif\n",
                encoding="utf-8",
            )
            (root / "base.c").write_text(
                '#include "base.h"\n\nint base_open(const char *path) {\n    return path != 0;\n}\n',
                encoding="utf-8",
            )
            (root / "user.c").write_text(
                '#include "user.h"\n\n#include <string.h>\n#include "base.h"\n\n'
                "int user_run(const char *path) {\n"
                "    base_item_t item;\n"
                "    int n = BASE_SQUARE(BASE_LIMIT);\n"
                "    memset(&item, 0, sizeof(item));\n"
                "    return base_open(path) + n;\n"
                "}\n",
                encoding="utf-8",
            )
            (root / "user.h").write_text(
                "#ifndef USER_H\n#define USER_H\n\nint user_run(const char *path);\n\n#endif\n",
                encoding="utf-8",
            )

            config = RunConfig(project_root=str(root), output_root=str(root / "out"))
            from agent2.nodes.scan_tool import scan_project

            state = {"config": config, "modules": scan_project(root)}
            state.update(dependencies_tool(state))
            background = state["dependencies"]["M02"]  # M01 = base，M02 = user

            self.assertIn("BASE_LIMIT", background)
            self.assertIn("BASE_SQUARE", background)
            self.assertIn("base_item_t", background)
            self.assertIn("int base_open(const char *path);", background)
            # 标准库符号不属于项目内部依赖，一个都不该出现
            for external in ("memset", "string.h", "sizeof"):
                self.assertNotIn(external, background)


class ChunkBackgroundTest(unittest.TestCase):
    """子块检视的背景信息必须**带上模块的外部依赖**。

    切得越细越容易看不见跨模块的类型与契约，而那份清单是扫描阶段专门为这个模块
    整理好的——丢掉它，切分反而让检视更盲目。
    """

    def test_module_dependencies_survive_into_chunk_background(self) -> None:
        from agent2.nodes.chunk_reviews_llm import _chunk_background

        chunk = {"name": "调度", "purpose": "分发请求", "functions": ["run"]}
        dependency_text = "【函数签名】\n  int base_open(const char *path);    （定义于 base.h:6）"

        with_deps = _chunk_background(dependency_text, chunk, 3)
        self.assertIn("base_open", with_deps, "外部依赖必须出现在子块的背景信息里")
        self.assertIn("调度", with_deps)

        without_deps = _chunk_background("", chunk, 3)
        self.assertNotIn("【函数签名】", without_deps)
        self.assertIn("子块", without_deps)


class PlanTest(unittest.TestCase):
    def _run_plan(self, chunks: list[dict]) -> tuple[dict, dict]:
        tmp = tempfile.TemporaryDirectory(prefix="agent2-plan-")
        self.addCleanup(tmp.cleanup)
        project = _write_project(Path(tmp.name))
        config = RunConfig(project_root=str(project), output_root=str(Path(tmp.name) / "out"))

        from agent2.nodes.architecture_check_tool import architecture_check_tool
        from agent2.nodes.call_graph_tool import call_graph_tool
        from agent2.nodes.symbol_table_tool import symbol_table_tool

        module = {
            "id": "M01", "name": "mod", "files": ["mod.c", "mod.h"],
            "lines": 30, "chars": 300, "estimated_tokens": 100,
        }
        state: dict = {"config": config, "module": module, "architecture": {"chunks": chunks}, "plan_rounds": 0}
        state.update(symbol_table_tool(state))
        state.update(call_graph_tool(state))
        state.update(architecture_check_tool(state))
        return state, {"config": config, "module": module}

    def test_missing_function_goes_to_residual_and_cycle_split_is_sent_back(self) -> None:
        # 方案把相互递归的两个函数拆到了两块，还漏掉了 helper
        state, context = self._run_plan(
            [
                {"name": "第一步", "purpose": "只放 step_a", "functions": ["step_a"]},
                {"name": "第二步", "purpose": "只放 step_b", "functions": ["step_b"]},
            ]
        )
        self.assertTrue(state["plan_pending"], "拆散循环调用组与漏认领都该退回重做")
        self.assertIn("循环调用组被拆到", state["plan_feedback"])

        # 重做到上限仍不达标时，要带着违规继续出结果，而不是让模块拿不到报告
        state["plan_rounds"] = 2
        from agent2.nodes.architecture_check_tool import architecture_check_tool

        result = architecture_check_tool(state)
        self.assertFalse(result["plan_pending"])
        self.assertTrue(result["plan_violations"], "到上限后违规要如实记录")
        roles = {chunk["name"]: chunk for chunk in result["chunks"]}
        self.assertIn("残余", roles)
        self.assertEqual(roles["残余"]["functions"], ["helper"])

    def test_every_line_is_covered_by_some_chunk(self) -> None:
        state, context = self._run_plan(
            [
                {"name": "A", "purpose": "两个递归函数", "functions": ["step_a", "step_b"]},
                {"name": "B", "purpose": "工具函数", "functions": ["helper"]},
            ]
        )
        self.assertFalse(state["plan_pending"])
        total = len((Path(context["config"].project_root) / "mod.c").read_text(encoding="utf-8").splitlines())
        covered = set()
        for chunk in state["chunks"]:
            for span in chunk["ranges"]:
                covered |= set(range(span["start"], span["end"] + 1))
        self.assertEqual(covered, set(range(1, total + 1)), "模块每一行都必须归属某个子块")


class LargeGraphTest(unittest.TestCase):
    """端到端：跑通大图，顺带验证子块并发合并与探测两轮。"""

    def _run(self, rebuttals: list[dict]) -> tuple[dict, _Client]:
        tmp = tempfile.TemporaryDirectory(prefix="agent2-largegraph-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        project = _write_project(root)
        config = RunConfig(project_root=str(project), output_root=str(root / "out"))
        module = {
            "id": "M01", "name": "mod", "files": ["mod.c", "mod.h"],
            "lines": 2001, "chars": 20000, "estimated_tokens": 6000,
        }

        client = _Client(
            architecture={
                "patterns": ["循环调用组"],
                "summary": "围绕两个相互递归的解析函数组织。",
                "strategy": "按调用组切分。",
                "chunks": [
                    {"name": "递归组", "purpose": "互相调用的两个函数", "functions": ["step_a", "step_b"]},
                    {"name": "工具", "purpose": "辅助函数", "functions": ["helper"]},
                ],
            },
            review={
                "findings": [
                    {
                        "file": "mod.c", "line": 19, "severity": "严重",
                        "description": "malloc 未判空即解引用", "evidence": "p[0] = '\\0';",
                        "suggestion": "分配后判空",
                    }
                ]
            },
            rebuttals=rebuttals,
        )
        original = llm._build_client
        llm._build_client = lambda spec, base_url=None: client
        self.addCleanup(setattr, llm, "_build_client", original)

        state = build_large_review_graph().invoke({"config": config, "module": module})
        return state, client

    def test_probe_round_then_conclusion(self) -> None:
        # 第一轮只判一条、另一条点菜取证；第二轮凭证据把剩下那条判掉
        state, client = self._run(
            [
                {"items": [{"id": "f1", "conclusion": "维持", "reason": "就地可判"}],
                 "queries": [{"kind": "callers", "target": "step_b"}]},
                {"items": [{"id": "f2", "conclusion": "反驳", "reason": "上游已校验"}], "queries": []},
            ]
        )

        self.assertFalse(state.get("failed"))
        self.assertEqual(state["probe_rounds"], 2, "应当走完两轮：一轮取证、一轮定论")
        conclusions = {item["id"]: item["conclusion"] for item in state["rebuttals"]}
        self.assertEqual(conclusions, {"f1": "维持", "f2": "反驳"})

        # 取证留痕要落盘，且证据确实是被查过的
        evidence = read_json(client_evidence_path(state))
        self.assertEqual(evidence[0]["queries"][0]["kind"], "callers")
        self.assertIn("step_b", evidence[0]["results"][0]["result"])

        report = read_text(Path(state["report_path"]))
        self.assertIn("## 分解", report, "大模块的报告要交代分解情况")
        self.assertIn("递归组", report)


def client_evidence_path(state: dict) -> Path:
    return Path(state["config"].output_root) / "M01" / "probe_rounds.json"


if __name__ == "__main__":
    unittest.main()
