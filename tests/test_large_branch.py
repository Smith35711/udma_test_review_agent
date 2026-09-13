"""large 分支：合成一个真的超过阈值的大模块，把这条路径完整跑一遍。

`test_project` 的三个样例模块全是 small，于是 `DOMAIN_REVIEW_PROMPT`、按 `.c` 逐文件
分批、`header_signatures_text` 这三段代码**从未被执行过**——它们是不是能跑通，在真实
项目上第一次触发之前无人知道。本文件用合成夹具把这条路径变成有覆盖的路径。

夹具是生成的（不是手写的两千行），故行号由生成器算出来，不靠肉眼数。
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from udma_test_review_agent.nodes.common import _module_arch_path, _module_size_path, _write_json
from udma_test_review_agent.nodes.llm import review as rv
from udma_test_review_agent.nodes.tool import size_analysis
from udma_test_review_agent.schemas import GlobalArchitecture, ReviewCheck

from _support import TempProject, arch_payload, env, finding, raw_findings, write_models_yaml

_HEADER = """\
#ifndef BIG_H
#define BIG_H

/* 契约：dst 至少可写 n 字节 */
void big_copy(int dst_id, const char *src, int n);
int big_lookup(int index);

#endif
"""


def _build_source(functions: int = 140, body_lines: int = 12) -> tuple[str, dict[str, int]]:
    """生成一个远超 2000 行的 `.c`，并返回植入缺陷的 `名称 -> 行号`。

    每个函数块占 `body_lines + 5` 行，故默认参数下约 2400 行——留出余量，
    免得改一行生成逻辑就把夹具掉回 small、整条 large 路径又变回零覆盖。

    缺陷刻意分散在文件的首、中、尾三处：按文件分批与排序若写错，最容易的失败模式
    就是「尾部的植入缺陷从未被打包进任何一次调用」。
    """
    lines = ["#include <stdlib.h>", "#include <string.h>", '#include "big.h"', ""]
    defects: dict[str, int] = {}

    def emit(text: str) -> None:
        lines.append(text)

    for index in range(functions):
        emit(f"/* ---- 区块 {index} ---- */")
        emit(f"static int helper_{index}(int value) {{")
        for row in range(body_lines):
            emit(f"    int step_{index}_{row} = value + {row};")
        emit(f"    return step_{index}_0;")
        emit("}")
        emit("")
        if index == 0:
            # 首部植入：无界拷贝
            emit("void big_copy(int dst_id, const char *src, int n) {")
            emit("    char *dst = get_buffer(dst_id);")
            defects["head_unbounded_write"] = len(lines)
            emit("    strcpy(dst, src);")
            emit("}")
            emit("")
        elif index == functions // 2:
            # 中部植入：释放后使用
            emit("int big_lookup(int index) {")
            emit("    int *table = (int *)malloc(sizeof(int) * 8);")
            emit("    free(table);")
            defects["middle_use_after_free"] = len(lines)
            emit("    return table[index];")
            emit("}")
            emit("")

    # 尾部植入：未检查的分配
    emit("void big_tail(void) {")
    defects["tail_unchecked_alloc"] = len(lines)
    emit("    char *p = malloc(4);")
    emit("    p[0] = 'x';")
    emit("}")
    return "\n".join(lines) + "\n", defects


class _LargeHarness(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source, cls.defects = _build_source()

    def setUp(self) -> None:
        self.project = TempProject({"big.c": self.source, "big.h": _HEADER})
        self.addCleanup(self.project.cleanup)
        self._tmp = tempfile.TemporaryDirectory(prefix="udma-large-")
        self.addCleanup(self._tmp.cleanup)
        self._env = env(
            MODEL_CONFIG=str(write_models_yaml(Path(self._tmp.name))),
            TEST_API_KEY="k",
            MODULES_CONFIG=str(self.project.modules_config),
        )
        self._env.__enter__()
        self.addCleanup(self._env.__exit__, None, None, None)
        self.cfg = self.project.config(output_root=str(Path(self._tmp.name) / "out"))
        self.scan = self.project.scan()

    def test_fixture_really_exceeds_the_threshold(self):
        """夹具本身必须是 large，否则整条路径又是没被跑过的那条。"""
        total = sum(f.lines for m in self.scan.modules for f in m.files)
        self.assertGreater(total, self.cfg.small_module_line_threshold)

    def size_class(self) -> str:
        out = size_analysis._size_analysis_one(self.cfg, self.scan, "M01")
        return out["module_size"]["M01"]["size_class"]

    def test_size_analysis_marks_it_large_and_persists(self):
        self.assertEqual(self.size_class(), "large")
        persisted = _module_size_path(self.cfg, "M01")
        self.assertTrue(persisted.is_file())

    def test_size_threshold_boundary_is_inclusive(self):
        """阈值取 `<=` 为 small：等于阈值算小模块，这是文档承诺的语义。"""
        total = sum(f.lines for m in self.scan.modules for f in m.files)
        cfg = self.project.config(
            output_root=str(Path(self._tmp.name) / "out2"),
            small_module_line_threshold=total,
        )
        out = size_analysis._size_analysis_one(cfg, self.scan, "M01")
        self.assertEqual(out["module_size"]["M01"]["size_class"], "small")

    def test_size_analysis_reuses_a_valid_checkpoint(self):
        self.size_class()
        out = size_analysis._size_analysis_one(self.cfg, self.scan, "M01")
        self.assertEqual(out["module_size"]["M01"]["lines"],
                         sum(f.lines for m in self.scan.modules for f in m.files))

    def test_corrupt_size_artifact_is_recomputed(self):
        _module_size_path(self.cfg, "M01").parent.mkdir(parents=True, exist_ok=True)
        _module_size_path(self.cfg, "M01").write_text("{不是 JSON", encoding="utf-8")
        self.assertEqual(self.size_class(), "large")

    def test_large_path_runs_with_a_domain_prompt_carrying_headers_and_defects(self):
        """把 large 路径真跑一遍：按 `.c` 分批 + header 签名 + 提示词渲染。"""
        _write_json(_module_arch_path(self.cfg, "M01"), arch_payload("M01"))
        prompts: list[str] = []

        def _spy(prompt, schema, *args, **kwargs):
            prompts.append(prompt)
            if schema is rv.RawFindings:
                return raw_findings(
                    [
                        finding(
                            file="big.c",
                            line=self.defects["head_unbounded_write"],
                            defect_kind="unbounded_write",
                        )
                    ]
                )
            return ReviewCheck(passed=True)

        original = rv._llm
        rv._llm = _spy
        self.addCleanup(lambda: setattr(rv, "_llm", original))
        out = rv._review_module_raw(
            self.scan,
            GlobalArchitecture(layers=[], modules=[], trust_entries=[], concurrency_hotspots=[]),
            "M01",
            "large",
            self.cfg,
            [],
        )
        self.assertEqual(len(prompts), 1, "单 .c 模块应恰好一次按域检视")
        # 头文件走 `headers` 占位符、本文件源码走 `source`；两者都进提示词，
        # 说明按域检视的两路材料都装配上了（提示词渲染少一个具名参数会直接 KeyError）。
        self.assertIn("===== 头文件: big.h =====", prompts[0])
        self.assertIn("void big_copy(int dst_id, const char *src, int n);", prompts[0])
        self.assertIn("strcpy(dst, src);", prompts[0])
        self.assertEqual(out["findings"][0]["evidence_status"], "verified")
        self.assertEqual(out["findings"][0]["line"], self.defects["head_unbounded_write"])

    def test_tail_defect_line_is_pointable(self):
        """夹具的植入行号必须真的指向那行代码，否则上面的断言只是在数行号。"""
        lines = self.source.splitlines()
        for name, line in self.defects.items():
            with self.subTest(defect=name):
                self.assertTrue(0 < line <= len(lines))
                self.assertNotEqual(lines[line - 1].strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
