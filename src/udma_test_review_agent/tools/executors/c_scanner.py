"""S0 纯文本 C 项目扫描器：不依赖编译，全部为源码文本级分析。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

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

DEFAULT_EXCLUDE_DIRS = frozenset({".git", ".svn", "__pycache__", "build", "out", "dist", "node_modules", ".venv"})
"""默认排除目录。仅作为未显式传入 `exclude_dirs` 时的兜底；生产路径由
`ReviewConfig.excludes`（唯一真相来源）经 `scan_project` 传入。"""
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


def _iter_source_files(root: Path, exclude_dirs: frozenset[str]) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in C_SUFFIXES:
            continue
        if any(part in exclude_dirs for part in path.relative_to(root).parts):
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


def _extract_build_flags(root: Path, exclude_dirs: frozenset[str]) -> list[str]:
    flags: list[str] = []
    for name in ("Makefile", "CMakeLists.txt", "meson.build"):
        for marker in root.rglob(name):
            if any(part in exclude_dirs for part in marker.relative_to(root).parts):
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
    exclude_dirs: Iterable[str] | None = None,
) -> ScanResult:
    """扫描项目为文件级模块；module_ids 为 {相对 .c 路径: 模块标识} 映射。

    exclude_dirs 为要跳过的目录名集合（按路径段匹配）；None 时取 DEFAULT_EXCLUDE_DIRS。
    """
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ValueError(f"被测项目目录不存在: {root}")
    excludes = frozenset(exclude_dirs) if exclude_dirs is not None else DEFAULT_EXCLUDE_DIRS
    logger.info("[S0] 扫描项目 %s", root)
    logger.debug("[S0] 扫描参数 排除目录=%s", sorted(excludes))

    source_files = _iter_source_files(root, excludes)
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
    build_flags = _extract_build_flags(root, excludes)

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


DECL_NAME_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
"""行内的标识符加左括号形态，用于在一行里认出被声明/调用的函数名。"""


def containing_function_name(scan: ScanResult, rel_path: str, line: int) -> str:
    """返回包含该行的函数名；未命中返回空串。

    纯内存查表（`scan.functions` 已带全部函数的行区间），**不读盘**——调用方常在
    逐条 finding 的循环里用它，而 `containing_function_code` 每次命中都要读一遍源码。
    """
    for func in scan.functions:
        if func.file == rel_path and func.start_line <= line <= func.end_line:
            return func.name
    return ""


def line_text_at(
    scan: ScanResult, rel_path: str, line: int, *, cache: dict[str, list[str]] | None = None
) -> str:
    """取该行源码原文；文件不可读或行号越界返回空串。"""
    lines = _file_lines(scan, rel_path, cache)
    return lines[line - 1] if 0 < line <= len(lines) else ""


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


def upstream_call_sites(
    scan: ScanResult,
    function_name: str,
    *,
    context: int = 8,
    cache: dict[str, list[str]] | None = None,
) -> list[str]:
    """返回上游调用点的**代码**（不只是函数名）。

    反驳阶段此前只拿到 `foo (file.c)` 这样的名字：一条契约类断言是否成立，取决于调用点
    究竟做了什么——是否校验了长度、是否持有锁、返回值有没有被检查。只给函数名，模型
    无从核对，只能倾向 refuted，这是漏报的直接来源之一。

    定位策略：先在本文件里按 `名字(` 找调用行；找不到（宏包装、函数指针、跨行调用等
    文本级扫描器的固有盲区）则回退给出调用方函数体的开头一段。**尽力而为**，
    取不到就返回空列表，绝不编造。
    """
    sites: list[str] = []
    # 被调用函数自身的定义行不是调用点：同文件调用时，正则会在定义处先命中，
    # 于是「上游调用点」会指向被反驳函数自己的签名——比不给代码更误导。
    definition_lines = {
        (f.file, f.start_line) for f in scan.functions if f.name == function_name
    }
    for edge in scan.call_graph:
        if edge.callee != function_name:
            continue
        lines = _file_lines(scan, edge.caller_file, cache)
        hit = 0
        pattern = re.compile(rf"\b{re.escape(function_name)}\s*\(")
        for idx, text in enumerate(lines, 1):
            stripped = text.strip()
            if not stripped or stripped.startswith(("/*", "*", "//")):
                continue
            if (edge.caller_file, idx) in definition_lines:
                continue
            if pattern.search(text):
                hit = idx
                break
        if hit:
            start = max(1, hit - context)
            end = min(len(lines), hit + context)
            body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))
            sites.append(f"{edge.caller} @{edge.caller_file}:{hit}\n{body}")
            continue
        func = next(
            (f for f in scan.functions if f.file == edge.caller_file and f.name == edge.caller), None
        )
        if func is None:
            continue
        end = min(func.end_line, func.start_line + 2 * context)
        body = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(func.start_line, end + 1) if i <= len(lines))
        sites.append(f"{edge.caller} @{edge.caller_file}:{func.start_line}（未定位到精确调用行，给出调用方函数体）\n{body}")
    return sites


def function_excerpt(
    scan: ScanResult,
    rel_path: str,
    line: int,
    *,
    context: int = 60,
    cache: dict[str, list[str]] | None = None,
) -> tuple[str, str]:
    """返回 (函数名, 以 `line` 为中心的带行号代码片段)。

    **必须以问题行为中心，而不是从函数头截断。** 原实现把函数源码截到 6000 字符、
    从头保留：长函数里位于尾部的 finding，提示词里根本不含它所指的语句，
    反驳只能写「证据不足」或误判——这直接打在召回上。样例工程的函数只有 3-5 行，
    所以此前从未暴露，真实工程上必然触发。
    """
    name, _ = containing_function_code(scan, rel_path, line)
    lines = _file_lines(scan, rel_path, cache)
    if not lines:
        return name, ""
    start = max(1, line - context)
    end = min(len(lines), line + context)
    excerpt = "\n".join(f"{i:>5}| {lines[i - 1]}" for i in range(start, end + 1))
    return name, excerpt


def normalize_path(scan: ScanResult, rel_path: str) -> str:
    """把 finding 里的文件路径归一到扫描产物中的相对路径；无法唯一确定时返回空串。

    模型会写成 `./buffer.c`、`buffer.c`、`src/buffer.c` 等多种形式。不归一化会让
    位置校验把所有 finding 判为「文件不在扫描范围」，看起来像灾难性失败，
    实际只是路径写法差异。
    """
    candidate = str(rel_path or "").replace("\\", "/").strip()
    while candidate.startswith("./"):
        candidate = candidate[2:]
    known = {f.path for m in scan.modules for f in m.files}
    if candidate in known:
        return candidate
    base = PurePosixPath(candidate).name
    matches = sorted(p for p in known if PurePosixPath(p).name == base)
    return matches[0] if len(matches) == 1 else ""


def _file_lines(scan: ScanResult, rel_path: str, cache: dict[str, list[str]] | None) -> list[str]:
    """按文件读取源码行；带缓存以免逐条 finding 重复读盘。"""
    store = cache if cache is not None else {}
    if rel_path not in store:
        root = Path(scan.project_root)
        try:
            store[rel_path] = (root / rel_path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            store[rel_path] = []
    return store[rel_path]


def classify_location(
    scan: ScanResult, rel_path: str, line: int, *, cache: dict[str, list[str]] | None = None
) -> dict[str, str]:
    """判定 finding 位置的可核验性，返回 `{status, reason, path, line_text}`。

    **三值而非二值**，因为「文本级扫描器找不到所属函数」与「位置根本不存在」是两回事：

    - `verified`       —— 文件在扫描范围内、行号有效、且落在某个已识别函数体内；
    - `unattributable` —— 位置真实存在，但定位不到所属函数。头文件声明（`void f(int);`
      以 `;` 结尾，不满足函数签名识别条件）、文件作用域的全局/静态表、宏、
      以及**签名换行的多行函数**都会落在这里。这是扫描器的能力边界，**不是模型错误**：
      按「无效」处理会把整类真实缺陷（头文件契约缺陷等）系统性丢掉；
    - `invalid`        —— 文件不在扫描范围、行号越界、或该行是空行/纯注释。只有这一类
      属于模型的定位错误。
    """
    path = normalize_path(scan, rel_path)
    if not path:
        return {"status": "invalid", "reason": "文件不在扫描范围内", "path": "", "line_text": ""}

    line_count = next((f.lines for m in scan.modules for f in m.files if f.path == path), 0)
    if line < 1 or (line_count and line > line_count):
        return {"status": "invalid", "reason": f"行号 {line} 超出文件范围(1..{line_count})", "path": path, "line_text": ""}

    lines = _file_lines(scan, path, cache)
    text = lines[line - 1] if 0 < line <= len(lines) else ""
    stripped = text.strip()
    if not stripped:
        return {"status": "invalid", "reason": "该行为空行", "path": path, "line_text": text}
    if stripped.startswith(("/*", "*", "//")):
        return {"status": "invalid", "reason": "该行为注释", "path": path, "line_text": text}

    name, _ = containing_function_code(scan, path, line)
    if name:
        return {"status": "verified", "reason": f"位于函数 {name} 内", "path": path, "line_text": text}
    return {
        "status": "unattributable",
        "reason": "位置存在但文本级扫描器无法定位所属函数（头文件声明/宏/多行签名/文件作用域）",
        "path": path,
        "line_text": text,
    }
