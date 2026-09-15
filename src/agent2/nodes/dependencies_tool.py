"""模块间依赖（确定性）：把"本模块用到的、定义在**项目其他模块**里的东西"整理成背景信息。

需求要的正是**内部依赖**：标准库与系统调用一概不要——它们不属于被测项目，
对判断本模块的契约、边界与所有权没有帮助，只会白占上下文。

产出三样：宏（含宏函数）、函数的完整签名、结构体与类型定义。这份文本正是各节点
一直预留着的「背景信息」段的来源——有了它，检视时才能看见跨模块的前置条件与
类型约定，而不是只看得到本模块自己那几个文件。

**前提**：需要先跑过扫描（模块清单是它的输入）。清单缺了就不产出，不猜。
"""

from __future__ import annotations

from pathlib import Path

from .. import csymbols
from ..artifacts import read_text, write_json
from ..log import get_logger
from ..state import ScanState

logger = get_logger(__name__)

_SECTION_ORDER = ("宏", "函数", "类型")
_SECTION_TITLES = {"宏": "宏与宏函数", "函数": "函数签名", "类型": "结构体与类型定义"}


def _project_index(modules: list[dict], root: Path) -> dict[str, dict]:
    """建立全项目的符号索引：名字 → 定义条目（含它属于哪个模块）。

    同一个名字被多个模块定义时**整条丢弃**：指向不明的依赖写进背景信息只会误导，
    与调用图对同名函数的处理一致。
    """
    index: dict[str, dict] = {}
    ambiguous: set[str] = set()
    for module in modules:
        # **只索引头文件**：跨模块能用到的东西必须出现在头文件里。顺带避开一个坑——
        # 源文件里的调用语句（`return bm_init(name);`）形似声明，会把同一个符号
        # 记到两个模块名下，被当成"重名"而整条丢掉。
        for name in [item for item in module["files"] if item.endswith(".h")]:
            text = read_text(root / name)
            entries = (
                csymbols.parse_macro_entries(text, name)
                + csymbols.parse_declaration_entries(text, name)
                + csymbols.parse_type_entries(text, name)
            )
            for entry in entries:
                key = entry["name"]
                if key in index and index[key]["module"] != module["id"]:
                    ambiguous.add(key)
                    continue
                index.setdefault(key, dict(entry, module=module["id"]))
    for key in ambiguous:
        index.pop(key, None)
    return index


def _used_names(module: dict, root: Path) -> set[str]:
    used: set[str] = set()
    for name in module["files"]:
        used |= set(csymbols._CALL_RE.findall(read_text(root / name)))
        used |= set(csymbols._IDENT_RE.findall(read_text(root / name)))
    return used


def _render(module_id: str, dependencies: list[dict]) -> str:
    if not dependencies:
        return ""
    lines = [f"本模块（{module_id}）用到以下定义在**项目其他模块**中的符号，可作为前置条件与类型约定的参考：", ""]
    for kind in _SECTION_ORDER:
        members = [item for item in dependencies if item["kind"] == kind]
        if not members:
            continue
        lines.append(f"【{_SECTION_TITLES[kind]}】")
        for item in sorted(members, key=lambda entry: entry["name"]):
            lines.append(f"  {item['text']}    （定义于 {item['file']}:{item['line']}）")
        lines.append("")
    return "\n".join(lines).strip()


def dependencies_tool(state: ScanState) -> dict:
    config = state["config"]
    modules = state.get("modules") or []
    root = Path(config.project_root)

    index = _project_index(modules, root)
    dependencies: dict[str, str] = {}
    for module in modules:
        used = _used_names(module, root)
        found = [
            entry for key, entry in index.items() if key in used and entry["module"] != module["id"]
        ]
        dependencies[module["id"]] = _render(module["id"], found)

    write_json(config.dependencies_path, dependencies)
    logger.info(
        "模块间依赖: 模块数=%d 有外部依赖的模块=%d 索引符号=%d",
        len(modules), sum(1 for text in dependencies.values() if text), len(index),
    )
    return {"dependencies": dependencies}