"""S0 纯文本 C 项目扫描器：不依赖编译，全部为源码文本级分析。"""

from __future__ import annotations

import re
from pathlib import Path

from ...log import get_logger
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


def _module_name(rel_path: str) -> str:
    parts = Path(rel_path).parts
    return parts[0] if len(parts) > 1 else "(root)"


def scan_project(project_root: str, small_token_threshold: int) -> ScanResult:
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ValueError(f"被测项目目录不存在: {root}")
    logger.info("[S0] 扫描项目 %s", root)

    source_files = _iter_source_files(root)
    functions: list[FunctionInfo] = []
    include_edges: list[IncludeEdge] = []
    module_map: dict[str, dict] = {}

    rel_paths = {str(p.relative_to(root)) for p in source_files}
    for path in source_files:
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="replace")
        module = _module_name(rel)
        info = module_map.setdefault(
            module,
            {"path": str(Path(rel).parent) if Path(rel).parent != Path(".") else ".", "files": [], "chars": 0},
        )
        info["files"].append(SourceFileInfo(path=rel, lines=text.count("\n") + 1, chars=len(text)))
        info["chars"] += len(text)
        functions.extend(_extract_functions(text, rel))
        for line in text.splitlines():
            include = LOCAL_INCLUDE_RE.match(line)
            if not include:
                continue
            target = include.group(1)
            candidates = [path.parent / target, root / target]
            for flag in _extract_build_flags(root):
                if flag.startswith("-I"):
                    candidates.append(root / flag[2:] / target)
            resolved = next((c.resolve() for c in candidates if c.exists()), None)
            if resolved is None:
                continue
            dst = str(resolved.relative_to(root))
            if dst in rel_paths:
                include_edges.append(IncludeEdge(src=rel, dst=dst))

    modules = []
    for name, info in module_map.items():
        modules.append(
            ModuleInventory(
                name=name,
                path=info["path"],
                files=info["files"],
                chars=info["chars"],
                token_estimate=int(info["chars"] / 3.5),
            )
        )
    modules.sort(key=lambda m: m.name)

    defined = {f.name: f for f in functions}
    call_edges: list[CallEdge] = []
    seen = set()
    for func in functions:
        path = root / func.file
        body = path.read_text(encoding="utf-8", errors="replace").splitlines()
        body_text = "\n".join(body[func.start_line - 1 : func.end_line])
        for callee, callee_info in defined.items():
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
        build_flags=_extract_build_flags(root),
    )
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
