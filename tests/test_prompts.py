"""提示词共享片段的防分叉测试。

P6 把三份检视提示词里逐字相同的片段收敛到 `prompts/_shared.py` 的**唯一副本**。
收敛本身只做了一次；真正会随时间腐坏的是**它保持收敛**——后来者若要微调某条误报边界，
最省事的做法就是在自己那份里改一行，于是三份判据又悄悄分叉，而分叉没有任何报错，
只表现为「有些路径复核得松、有些路径复核得紧」。

本文件把这件事变成会响的警报：共享常量必须是 `_shared` 里的**同一个对象**（`is`
而非 `==`——复制一份内容相同的字面量同样属于分叉），且每份提示词仍能完整渲染。
"""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from udma_test_review_agent.prompts import _shared
from udma_test_review_agent.prompts import (
    critical_paths,
    global_arch,
    module_arch,
    rebuttal,
    report,
    review,
    review_check,
    review_domain,
    review_focused,
    review_repair,
)

SHARED_NAMES = (
    "_ROLE",
    "_METHOD",
    "_CROSS_MODULE",
    "_FALSE_POSITIVE",
    "_SEVERITY",
    "_EVIDENCE",
    "_JSON_CONTRACT",
    "_FIELD_RULES",
    "_NEGATIVE",
    "_SELF_CHECK",
    "_EXAMPLES",
    "_EDGE",
)
"""两条以上路径逐字相同的常量。"""

_SHARED_EXCEPT_ROLE_AND_METHOD = tuple(
    name for name in SHARED_NAMES if name not in ("_ROLE", "_METHOD")
)

SHARED_MODULES = {
    review: SHARED_NAMES,
    review_domain: SHARED_NAMES,
    # 专项路径的角色段（并发与安全评审专家）与检视方法（只看两个维度）本来就不同，
    # 故这两段是它专有的，其余十段共用——这是有意的差异，不是漏改。
    review_focused: _SHARED_EXCEPT_ROLE_AND_METHOD,
}

PROMPT_MODULES = (
    review,
    review_domain,
    review_focused,
    review_repair,
    global_arch,
    module_arch,
    critical_paths,
    rebuttal,
    review_check,
    report,
)
"""全部提示词模块路径。新增提示词模块必须在此登记，否则占位符/调用点校验会静默漏掉它。"""


class SharedFragmentTest(unittest.TestCase):
    def test_shared_names_are_defined_once(self):
        for name in (*SHARED_NAMES, "_CHECKLIST_ALL", "_CHECKLIST_FOCUSED"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(_shared, name), f"_shared 缺少 {name}")

    def test_prompt_modules_reuse_the_same_objects(self):
        """按对象同一性（`is`）而非内容相等判定——内容相同的副本同样是分叉。"""
        for module, names in SHARED_MODULES.items():
            for name in names:
                with self.subTest(module=module.__name__, name=name):
                    self.assertIs(
                        getattr(module, name, None),
                        getattr(_shared, name),
                        f"{module.__name__} 里的 {name} 不是 _shared 的同一对象（疑似又抄了一份）",
                    )

    def test_focused_path_uses_the_shared_focused_checklist(self):
        self.assertIs(review_focused._CHECKLIST_FOCUSED, _shared._CHECKLIST_FOCUSED)

    def test_checklist_focused_is_composed_from_the_shared_items(self):
        """专项清单的公共部分必须真的来自共享项，而不是抄写一遍。"""
        self.assertIn(_shared._CONCURRENCY_ITEMS, _shared._CHECKLIST_FOCUSED)
        self.assertIn(_shared._TRUST_BOUNDARY_ITEMS, _shared._CHECKLIST_FOCUSED)
        self.assertIn(_shared._CONCURRENCY_ITEMS, _shared._CHECKLIST_ALL)
        self.assertIn(_shared._TRUST_BOUNDARY_ITEMS, _shared._CHECKLIST_ALL)

    def test_shared_items_appear_exactly_once_per_checklist(self):
        """同一段公共项在清单里只能出现一次：重复出现说明组装顺序错了。"""
        for checklist in (_shared._CHECKLIST_ALL, _shared._CHECKLIST_FOCUSED):
            with self.subTest(checklist=checklist[:20]):
                self.assertEqual(checklist.count(_shared._CONCURRENCY_ITEMS), 1)
                self.assertEqual(checklist.count(_shared._TRUST_BOUNDARY_ITEMS), 1)

    def test_focused_checklist_holds_only_the_two_domains(self):
        focused = _shared._CHECKLIST_FOCUSED
        self.assertIn("## 4.1 concurrency", focused)
        self.assertIn("## 4.2 trust_boundary", focused)
        for other in ("## 4.3", "## 4.4", "## 4.5"):
            self.assertNotIn(other, focused)

    def test_all_checklist_covers_the_seven_categories(self):
        for heading in (
            "## 4.1 memory",
            "## 4.2 concurrency",
            "## 4.3 trust_boundary",
            "## 4.4 resource",
            "## 4.5 logic",
            "## 4.6 macro",
            "## 4.7 ub",
        ):
            self.assertIn(heading, _shared._CHECKLIST_ALL)


_NODES_DIR = Path(__file__).resolve().parents[1] / "src" / "udma_test_review_agent" / "nodes"


def _dict_literals(tree: "object") -> dict[str, set[str]]:
    """模块内 `name = {"k": ..., ...}` 形式的字面量字典，用于展开 `format(**name)`。

    检视节点把公共参数装进 `common` 字典再展开，AST 只看得到 `checklist=` 这类显式
    关键字。不展开就会把「少传参数」漏判成通过——而这正是要防的失败模式。
    解析不到时退回「开放调用」处理（只做单向检查），不会误报。
    """
    import ast

    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        keys = {k.value for k in node.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = keys
    return found


def _format_calls() -> dict[str, list[tuple[set[str], bool]]]:
    """扫描节点源码，取「哪个提示词常量在哪些调用点用哪些具名参数填充」。

    返回 `{常量名: [(具名参数集合, 是否含 **展开)]}`。用 AST 而不是维护一份手写的参数
    白名单：白名单会随代码漂移，而它一旦漂移就只会报出假警报、最后被人加个 ignore
    关掉——那正是本文件要防的那类腐坏。
    """
    import ast

    calls: dict[str, list[tuple[set[str], bool]]] = {}
    for path in sorted(_NODES_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        literals = _dict_literals(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if not isinstance(target, ast.Attribute) or target.attr != "format":
                continue
            if not isinstance(target.value, ast.Name):
                continue
            names: set[str] = set()
            open_call = False
            for keyword in node.keywords:
                if keyword.arg is not None:
                    names.add(keyword.arg)
                    continue
                open_call = True
                if isinstance(keyword.value, ast.Name):
                    names |= literals.get(keyword.value.id, set())
            calls.setdefault(target.value.id, []).append((names, open_call))
    return calls


def _placeholders(template: str) -> set[str]:
    import string

    return {
        field.split(".")[0].split("[")[0]
        for _, field, _, _ in string.Formatter().parse(template)
        if field
    }


def _unescape(text: str) -> str:
    """`str.format` 之后花括号不再转义；共享片段里的 `{{` 是给 format 用的。"""
    return text.replace("{{", "{").replace("}}", "}")


class PromptRenderingTest(unittest.TestCase):
    """渲染面：模板缺参数会抛 KeyError，多传参数则被 `str.format` 静默忽略。"""

    def setUp(self) -> None:
        self.calls = _format_calls()

    def test_every_prompt_constant_has_a_call_site(self):
        for module in PROMPT_MODULES:
            for name in (n for n in dir(module) if n.endswith("_PROMPT")):
                with self.subTest(module=module.__name__, prompt=name):
                    self.assertIn(name, self.calls, f"{name} 没有任何 .format() 调用点")

    def test_placeholders_and_passed_arguments_agree(self):
        """逐调用点比对，双向：少传会 KeyError，多传说明提示词与喂给它的数据已经脱节。"""
        for module in PROMPT_MODULES:
            for name in (n for n in dir(module) if n.endswith("_PROMPT")):
                template = getattr(module, name)
                placeholders = _placeholders(template)
                for index, (kwargs, open_call) in enumerate(self.calls.get(name, [])):
                    with self.subTest(prompt=name, call=index):
                        # 少传参数这一向永远成立：有任何占位符没被填上就是真 bug。
                        self.assertEqual(
                            placeholders - kwargs,
                            set(),
                            f"{name} 第 {index} 个调用点少传参数（将抛 KeyError）",
                        )
                        if open_call:
                            # `format(**common)` 里到底有什么，静态看不全；解不出键名时
                            # 只做上面那一向，免得把「多传」判成误报。故这些调用点由
                            # `test_open_calls_resolve_to_the_template` 反向盯住。
                            continue
                        self.assertEqual(
                            kwargs - placeholders,
                            set(),
                            f"{name} 第 {index} 个调用点多传了模板里没有的参数",
                        )

    def test_open_calls_resolve_to_the_template(self):
        """`format(**common)` 的字典字面量必须能静态解析出来，否则上面的检查形同虚设。

        数量对不上说明检视节点换了一种写法（比如从别处取字典），此时该调用点会悄悄
        退化成单向检查——这里把它变成会响的警报。
        """
        for module in PROMPT_MODULES:
            for name in (n for n in dir(module) if n.endswith("_PROMPT")):
                for index, (kwargs, open_call) in enumerate(self.calls.get(name, [])):
                    if not open_call:
                        continue
                    with self.subTest(prompt=name, call=index):
                        self.assertNotEqual(
                            kwargs, set(),
                            f"{name} 第 {index} 个调用点用了 ** 展开但键名解析不出来，"
                            "占位符校验会退化为单向检查",
                        )

    def test_all_prompts_render_cleanly(self):
        """真渲染一遍：格式化后不应残留 `{{`/`}}`（那是把转义写错的地方）。"""
        for module in PROMPT_MODULES:
            for name in (n for n in dir(module) if n.endswith("_PROMPT")):
                template = getattr(module, name)
                kwargs = {key: f"<{key}>" for key in _placeholders(template)}
                with self.subTest(prompt=name):
                    text = template.format(**kwargs)
                    self.assertTrue(text.strip())
                    self.assertNotIn("{{", text)
                    self.assertNotIn("}}", text)

    def test_review_prompt_keeps_the_shared_json_contract(self):
        text = review.REVIEW_PROMPT.format(
            module_arch="MA",
            global_context="GC",
            critical_paths="CP",
            checklist="CL",
            source="SRC",
        )
        self.assertIn(_unescape(_shared._JSON_CONTRACT), text)
        self.assertIn("MA", text)


class RebuttalGuardrailTest(unittest.TestCase):
    """反驳提示词里两条「不得推翻」的红线。

    它们不是措辞偏好，而是实测出来的漏报来源：`buffer.c:7` 的无界 `strcpy` 被驳回，
    理由正是「契约已把 dst 容量写成调用方前置条件」——而该接口根本无处表达这个前置
    条件，这恰恰是 finding 的核心断言。提示词一旦被人「顺手精简」掉这两节，漏报会
    静默回来，且没有任何别的地方会报警。
    """

    def test_precondition_cannot_serve_as_counter_evidence(self):
        self.assertIn("6.1", rebuttal.REBUTTAL_BATCH_PROMPT)
        self.assertIn("不得判 refuted", rebuttal.REBUTTAL_BATCH_PROMPT)
        self.assertIn("契约已声明由调用方负责", rebuttal.REBUTTAL_BATCH_PROMPT)

    def test_missing_callsite_is_evidence_gap_not_refutation(self):
        self.assertIn("6.2", rebuttal.REBUTTAL_BATCH_PROMPT)
        self.assertIn("找不到调用方 ≠ 已推翻", rebuttal.REBUTTAL_BATCH_PROMPT)

    def test_exemption_is_scoped_to_concurrency(self):
        """豁免的第一条只针对加锁，不得被扩用到长度/容量/所有权等其它义务。"""
        self.assertIn("本项只豁免并发类断言", rebuttal.REBUTTAL_BATCH_PROMPT)


class ChannelBoundaryTest(unittest.TestCase):
    """检视提示词里「链路不完整只能降级并发/信任面」这条边界，三份路径必须一致。

    实测 `buffer.c:7` 的无界 `strcpy` 因「看不到调用方」被放进 observations 而整类
    移出结论表——observation 不进反驳、不计入统计。第八节虽已收窄，但第三节「检视
    方法」与第十节「字段级要求」旧文本仍在说「链路不完整即降级」，模型按就近的旧
    措辞行事。此处钉住三处口径一致，任一处被改回广谱降级即失败。
    """

    def test_method_says_incomplete_chain_is_not_a_demotion_reason(self):
        self.assertIn("链路不完整本身不是降级理由", _shared._METHOD)
        self.assertIn("未知调用方不等于证据不足", _shared._METHOD)

    def test_field_rules_scope_demotion_to_concurrency_and_trust(self):
        self.assertIn("不得因链路不完整而把 memory", _shared._FIELD_RULES)

    def test_evidence_clause_still_carries_the_narrow_rule(self):
        self.assertIn("其余类别（memory / ub / resource / logic / macro）不因链路不全而降级", _shared._EVIDENCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
