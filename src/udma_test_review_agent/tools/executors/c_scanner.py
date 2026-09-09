"""S0 纯文本 C 项目扫描器：不依赖编译，全部为源码文本级分析。"""

from __future__ import annotations

import re
from pathlib import Path

from ...log import get_logger
from ...module_map import id_sort_key
from ...schemas import (
    CallEdge,
    FunctionInfo,
    IncludeEdge,
    ModuleInventory,
    ScanResult,
    SourceFileInfo,
)

logger = get_logger(__name__)

EXCLUDE_DIRS = {".git", ".svn", "__pycache__", "build", "out", "dist", "node_modules", ".venv"}
C_SUFFIXES = {".c", ".h"}
C_KEYWORDS = {
    "if", "else", "for", "while", "switch", "return", "sizeof", "typedef", "do",
    "case", "goto", "break", "continue", "static", "extern", "const", "volatile",
    "unsigned", "signed", "register", "auto", "inline", "void", "char", "short",
    "int", "long", "float", "double", "struct", "union", "enum",
}
LOCAL_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"([^"]+)"')
CALL_RE_TMPL = r"\b{name}\s*\("


def _function_header_name(line: str) -> str | None:
    """token 级函数头判定：返回函数名，非函数头返回 None。"""
    stripped = line.strip()
    if not stripped or stripped.startswith(("#", "//", "/*", "*", '"')):
        return None
    if "(" not in line:
        return None
    tail = stripped.rstrip()
    if not (tail.endswith(")") or tail.endswith("){") or tail.endswith(") {")):
        return None
    head = line.split("(", 1)[0]
    if "=" in head:
        return None
    tokens = head.replace("*", " ").split()
    if len(tokens) < 2:
        return None
    name = tokens[-1]
    if not re.fullmatch(r"[A-Za-z_]\w*", name) or name in C_KEYWORDS:
        return None
    return name


def _iter_source_files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in C_SUFFIXES:
            continue
        if any(part in EXCLUDE_DIRS for part in path.relative_to(root).parts):
            continue
        files.append(path)
    return files


def _extract_functions(text: str, rel_path: str) -> list[FunctionInfo]:
    functions = []
    lines = text.splitlines()
    depth = 0
    in_body = False
    current_name = ""
    start_line = 0
    saw_brace = False
    for lineno, line in enumerate(lines, 1):
        if not in_body:
            name = _function_header_name(line)
            if name is None:
                continue
            current_name = name
            start_line = lineno
            in_body = True
            saw_brace = "{" in line
            depth = line.count("{") - line.count("}")
            if saw_brace and depth <= 0:
                functions.append(FunctionInfo(name=current_name, file=rel_path, start_line=start_line, end_line=lineno))
                in_body = False
            continue
        if in_body:
            saw_brace = saw_brace or "{" in line
            depth += line.count("{") - line.count("}")
            if saw_brace and depth <= 0:
                functions.append(FunctionInfo(name=current_name, file=rel_path, start_line=start_line, end_line=lineno))
                in_body = False
    return functions


def _extract_build_flags(root: Path) -> list[str]:
    flags: list[str] = []
    for name in ("Makefile", "CMakeLists.txt", "meson.build"):
        for marker in root.rglob(name):
            if any(part in EXCLUDE_DIRS for part in marker.relative_to(root).parts):
                continue
            try:
                text = marker.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            flags.extend(
                token for token in text.split() if token.startswith("-I") or token.startswith("-D")
            )
    return sorted(set(flags))[:100]


def scan_project(
    project_root: str,
    module_ids: dict[str, str] | None = None,
) -> ScanResult:
    """扫描项目为文件级模块；module_ids 为 {相对 .c 路径: 模块标识} 映射。"""
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ValueError(f"被测项目目录不存在: {root}")
    logger.info("[S0] 扫描项目 %s", root)
    logger.debug("[S0] 扫描参数 排除目录=%s", sorted(EXCLUDE_DIRS))

    source_files = _iter_source_files(root)
    rel_paths = {str(p.relative_to(root)) for p in source_files}
    c_paths = sorted(rel for rel in rel_paths if rel.endswith(".c"))
    logger.debug("[S0] 源码文件数=%d .c文件数=%d", len(source_files), len(c_paths))

    if module_ids is None:
        module_ids = {rel: f"M{i:02d}" for i, rel in enumerate(c_paths, 1)}
        logger.debug("[S0] 未提供模块映射，按路径自动编号")
    else:
        missing = [rel for rel in c_paths if rel not in module_ids]
        if missing:
            raise ValueError(f"以下 .c 文件未在 modules.yaml 中映射: {missing}")
        scanned = set(c_paths)
        extra = sorted(rel for rel in module_ids if rel not in scanned)
        if extra:
            raise ValueError(f"modules.yaml 映射的路径不在扫描范围内（须为已扫描的 .c）: {extra}")

    texts: dict[str, str] = {}
    functions: list[FunctionInfo] = []
    include_edges: list[IncludeEdge] = []
    build_flags = _extract_build_flags(root)

    for path in source_files:
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="replace")
        texts[rel] = text
        file_functions = _extract_functions(text, rel)
        functions.extend(file_functions)
        logger.debug("[S0] 文件 %s 行=%d 字符=%d 函数=%d", rel, len(text.splitlines()), len(text), len(file_functions))
        for line in text.splitlines():
            include = LOCAL_INCLUDE_RE.match(line)
            if not include:
                continue
            target = include.group(1)
            candidates = [path.parent / target, root / target]
            for flag in build_flags:
                if flag.startswith("-I"):
                    candidates.append(root / flag[2:] / target)
            resolved = next((c.resolve() for c in candidates if c.exists()), None)
            if resolved is None:
                logger.debug("[S0] include未解析 %s -> %s", rel, target)
                continue
            dst = str(resolved.relative_to(root))
            if dst in rel_paths:
                include_edges.append(IncludeEdge(src=rel, dst=dst))
                logger.debug("[S0] include %s -> %s", rel, dst)
            else:
                logger.debug("[S0] include指向项目外 %s -> %s", rel, dst)

    modules: list[ModuleInventory] = []
    for c_rel in c_paths:
        header_rel = str(Path(c_rel).with_suffix(".h"))
        file_rels = [c_rel] + ([header_rel] if header_rel in rel_paths else [])
        files = [
            SourceFileInfo(path=rel, lines=len(texts[rel].splitlines()), chars=len(texts[rel]))
            for rel in file_rels
        ]
        chars = sum(len(texts[rel]) for rel in file_rels)
        modules.append(
            ModuleInventory(
                module_id=module_ids[c_rel],
                name=module_ids[c_rel],
                path=c_rel,
                files=files,
                chars=chars,
                token_estimate=int(chars / 3.5),
            )
        )
    modules.sort(key=lambda m: id_sort_key(m.module_id))
    logger.debug(
        "[S0] 模块清单 %s",
        [(m.module_id, m.path, len(m.files), m.token_estimate) for m in modules],
    )

    defined = {f.name: f for f in functions}
    call_edges: list[CallEdge] = []
    seen = set()
    for func in functions:
        body_text = "\n".join(texts[func.file].splitlines()[func.start_line - 1 : func.end_line])
        for callee in defined:
            if callee == func.name:
                continue
            if re.search(CALL_RE_TMPL.format(name=re.escape(callee)), body_text):
                key = (func.name, callee)
                if key not in seen:
                    seen.add(key)
                    call_edges.append(CallEdge(caller=func.name, callee=callee, caller_file=func.file))

    result = ScanResult(
        project_root=str(root),
        modules=modules,
        include_graph=include_edges,
        call_graph=call_edges,
        functions=functions,
        build_flags=build_flags,
    )
    logger.debug("[S0] 构建参数=%s 调用边=%d", build_flags, len(call_edges))
    logger.info(
        "[S0] 完成: 模块=%d 文件=%d 函数=%d include边=%d 调用边=%d",
        len(modules),
        len(source_files),
        len(functions),
        len(include_edges),
        len(call_edges),
    )
    return result


def module_source_text(scan: ScanResult, module_name: str) -> str:
    root = Path(scan.project_root)
    module = next(m for m in scan.modules if m.name == module_name)
    chunks = []
    for file_info in module.files:
        text = (root / file_info.path).read_text(encoding="utf-8", errors="replace")
        numbered = "\n".join(f"{i:>5}| {line}" for i, line in enumerate(text.splitlines(), 1))
        chunks.append(f"===== 文件: {file_info.path} ({file_info.lines}行) =====\n{numbered}")
    return "\n\n".join(chunks)


def file_source_text(scan: ScanResult, rel_path: str) -> str:
    root = Path(scan.project_root)
    text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
    return "\n".join(f"{i:>5}| {line}" for i, line in enumerate(text.splitlines(), 1))


def header_signatures_text(scan: ScanResult, module_name: str) -> str:
    root = Path(scan.project_root)
    module = next(m for m in scan.modules if m.name == module_name)
    chunks = []
    for file_info in module.files:
        if not file_info.path.endswith(".h"):
            continue
        text = (root / file_info.path).read_text(encoding="utf-8", errors="replace")
        chunks.append(f"===== 头文件: {file_info.path} =====\n{text}")
    return "\n\n".join(chunks) if chunks else "(本模块无本地头文件)"


def containing_function_code(scan: ScanResult, rel_path: str, line: int) -> tuple[str, str]:
    """返回 (函数名, 带行号函数源码)；未命中返回空串。"""
    for func in scan.functions:
        if func.file == rel_path and func.start_line <= line <= func.end_line:
            root = Path(scan.project_root)
            text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
            body = text.splitlines()[func.start_line - 1 : func.end_line]
            numbered = "\n".join(
                f"{i:>5}| {line_}" for i, line_ in enumerate(body, func.start_line)
            )
            return func.name, numbered
    return "", ""


def upstream_callers(scan: ScanResult, function_name: str) -> list[str]:
    return [
        f"{edge.caller} ({edge.caller_file})"
        for edge in scan.call_graph
        if edge.callee == function_name
    ]
