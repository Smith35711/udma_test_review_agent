"""文本级 C 解析：从源码文本里取出函数、全局变量与调用点。

**本工程不调用编译器，也不做预处理**，这里全部是文本级匹配，结果只是**近似**。
已知会失真的情形，使用这些结果时必须知道：

- 宏未展开：`FUNC(x)` 形式的宏调用会被当成函数调用；由宏生成的函数定义识别不到。
- 条件编译未区分：`#ifdef` 两个分支里的定义都会被收进来，同一函数可能出现两次。
- 函数指针与间接调用不进调用图。
- 同名局部变量、类型名与函数名撞名时，会产生一条并不存在的调用边。
- 参数表里出现函数指针时，函数名可能取错（只回溯匹配一层括号）。

处理办法是"宁可少认，不要错认"：认不出的地方直接跳过，不做猜测性补全。
"""

from __future__ import annotations

import re
from pathlib import Path

_KEYWORDS = frozenset(
    {
        "if", "for", "while", "switch", "return", "sizeof", "do", "else",
        "case", "break", "continue", "goto", "defined", "union", "struct",
        "enum", "typedef",
    }
)


_DECLARATION_RE = re.compile(
    r"^[ \t]*(?:extern[ \t]+)?[A-Za-z_][\w \t\*]*?[ \t\*]([A-Za-z_]\w*)[ \t]*"
    r"\([^;{]*\)[ \t]*;",
    re.M,
)

_GLOBAL_RE = re.compile(
    # 类型与变量名之间：要么是空格，要么是指针星号（`session_t *g` 星号后可以没空格）。
    # 两者都要求至少一个分隔字符，否则 `intx` 会被拆成类型 `int` 与名字 `x` 而误报。
    r"^\s*(?:static[ \t]+)?(?:const[ \t]+)?(?:unsigned[ \t]+|signed[ \t]+)?"
    r"[A-Za-z_]\w*(?:[ \t]*\*+[ \t]*|[ \t]+)([A-Za-z_]\w*)[ \t]*(?:\[[^\]]*\])?[ \t]*(?:=|;)"
)

_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")

_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
"""所有标识符。判断"某个外部符号有没有被本模块用到"时用它做粗筛——
筛出来的是候选，真正决定要不要写进背景信息的是它在别处确有定义。"""


def strip_noise(text: str) -> str:
    """把注释与字符串/字符字面量替换成空格，**保留换行**——行号必须仍然准确。

    不这么做的话，注释和字面量里的括号会被当成代码括号，把函数边界算错；
    而字符字面量里的 `'{'` 更是会直接把花括号计数带偏。
    """
    chars = list(text)
    index, size = 0, len(text)
    while index < size:
        char = text[index]
        if char == "/" and index + 1 < size and text[index + 1] == "/":
            while index < size and text[index] != "\n":
                chars[index] = " "
                index += 1
        elif char == "/" and index + 1 < size and text[index + 1] == "*":
            chars[index] = chars[index + 1] = " "
            index += 2
            while index < size and not (text[index] == "*" and index + 1 < size and text[index + 1] == "/"):
                if text[index] != "\n":
                    chars[index] = " "
                index += 1
            for offset in (0, 1):
                if index + offset < size:
                    chars[index + offset] = " "
            index += 2
        elif char in "\"'":
            quote = char
            chars[index] = " "
            index += 1
            while index < size:
                if text[index] == "\\":
                    chars[index] = " "
                    if index + 1 < size:
                        chars[index + 1] = " "
                    index += 2
                    continue
                if text[index] == quote:
                    chars[index] = " "
                    index += 1
                    break
                if text[index] != "\n":
                    chars[index] = " "
                index += 1
        else:
            index += 1
    return "".join(chars)


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _name_before_brace(clean: str, brace_offset: int) -> tuple[str, int] | None:
    """取紧邻 `{` 之前的函数名与它在文本中的偏移；不是函数定义则返回 None。"""
    head = clean[:brace_offset].rstrip()
    if not head.endswith(")"):
        return None
    depth, index = 0, len(head) - 1
    while index >= 0:
        if head[index] == ")":
            depth += 1
        elif head[index] == "(":
            depth -= 1
            if depth == 0:
                break
        index -= 1
    if index < 0:
        return None
    match = re.search(r"([A-Za-z_]\w*)\s*$", head[:index])
    if not match or match.group(1) in _KEYWORDS:
        return None
    return match.group(1), match.start(1)


def parse_functions(text: str, filename: str) -> list[dict]:
    """取出文件里所有函数**定义**（顶层花括号块）。

    每项含名字、文件、起止行、行数与签名。正文不在这里返回——需要时按行范围
    回原文件切，符号表要保持精简。
    """
    clean = strip_noise(text)
    functions: list[dict] = []
    depth = 0
    opened: tuple[str, int] | None = None
    opened_offset = 0
    for offset, char in enumerate(clean):
        if char == "{":
            if depth == 0:
                opened = _name_before_brace(clean, offset)
                opened_offset = offset
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and opened is not None:
                name, name_offset = opened
                start_line = _line_of(clean, name_offset)
                end_line = _line_of(clean, offset)
                signature = " ".join(clean[name_offset:opened_offset].split())
                functions.append(
                    {
                        "name": name,
                        "file": filename,
                        "line": start_line,
                        "end_line": end_line,
                        "lines": end_line - start_line + 1,
                        "signature": signature,
                    }
                )
                opened = None
            if depth < 0:
                depth = 0
    return functions


def parse_declarations(text: str) -> list[str]:
    """取头文件里的函数声明名——它们就是模块的对外接口。"""
    return [match.group(1) for match in _DECLARATION_RE.finditer(strip_noise(text))]


def parse_globals(text: str, filename: str) -> list[dict]:
    """取文件作用域上的变量定义（函数体之外的、形如 `类型 名字 = ...;` 的行）。"""
    clean = strip_noise(text)
    globals_: list[dict] = []
    depth = 0
    statement_start = 0
    for offset, char in enumerate(clean):
        if char == "#" and depth == 0 and (offset == 0 or clean[offset - 1] == "\n"):
            # 预处理指令不带分号，不当成边界的话会把紧随其后的第一条语句一起吞掉
            line_end = clean.find("\n", offset)
            statement_start = len(clean) if line_end < 0 else line_end + 1
        elif char == "{":
            depth += 1
            if depth == 1:
                statement_start = offset + 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                statement_start = offset + 1
        elif char == ";" and depth == 0:
            # 只有函数体之外（depth == 0）的语句才可能是全局变量定义
            statement = clean[statement_start : offset + 1]
            match = _GLOBAL_RE.match(statement)
            if match:
                globals_.append(
                    {
                        "name": match.group(1),
                        "file": filename,
                        "line": _line_of(clean, statement_start + match.start(1)),
                    }
                )
            statement_start = offset + 1
    return globals_


def scan_calls(text: str, function: dict) -> list[dict]:
    """扫出函数体里所有「标识符 + 左括号」形态，已排除 C 关键字。

    这是**原始**结果，包含宏调用、被调库函数、以及与本模块函数同名的局部变量，
    调用方要自己按需要过滤。认不出的部分不是噪音——它们的数量正是"解析能看到
    多少"的度量，见 `symbol_table_tool` 里的 `unresolved_calls`。
    """
    clean = strip_noise(text)
    lines = clean.splitlines()
    body = "\n".join(lines[function["line"] - 1 : function["end_line"]])
    # 从函数体的左花括号之后开始扫：签名行里也带 `名字(`，
    # 不切掉的话每个函数都会多出一条指向自己的假调用边
    brace = body.find("{")
    body = body[brace + 1 :] if brace >= 0 else body
    calls: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for match in _CALL_RE.finditer(body):
        name = match.group(1)
        if name in _KEYWORDS:
            continue
        # `.foo(` 与 `->foo(` 是取成员，不是调用
        prefix = body[: match.start()].rstrip()
        if prefix.endswith(".") or prefix.endswith("->"):
            continue
        line = function["line"] + body.count("\n", 0, match.start())
        if (name, line) in seen:
            continue
        seen.add((name, line))
        calls.append({"callee": name, "line": line})
    return calls


def find_calls(text: str, function: dict, known_names: set[str]) -> list[dict]:
    """只保留能对应到本模块已知函数的调用——调用图的边必须都有实体可指。"""
    return [call for call in scan_calls(text, function) if call["callee"] in known_names]


_MACRO_RE = re.compile(r"^([ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[^\n]*)", re.M)

_TYPE_RE = re.compile(
    # 三条分支缺一不可：带花括号的 typedef 必须一路匹配到 `} 名字;`，
    # 只写 `typedef[\s\S]*?;` 会停在结构体内部第一个分号上，把定义截成半截
    r"^[ \t]*(?:"
    r"typedef[\s\S]*?\}\s*[A-Za-z_]\w*\s*;"
    r"|(?:struct|union|enum)\s+[A-Za-z_]\w*\s*\{[\s\S]*?\}\s*;"
    r"|typedef[^;{}]*;"
    r")",
    re.M,
)


def parse_macros(text: str) -> list[str]:
    """取本文件里定义过的宏名。判定"这个标识符是函数还是宏"要用到它。"""
    return [match.group(2) for match in _MACRO_RE.finditer(text)]


def parse_macro_entries(text: str, filename: str) -> list[dict]:
    """取宏定义**原文**（含宏函数），供模块间依赖的背景信息使用。"""
    return [
        {
            "name": match.group(2),
            "kind": "宏",
            "text": " ".join(match.group(1).split()),
            "file": filename,
            "line": _line_of(text, match.start(1)),
        }
        for match in _MACRO_RE.finditer(strip_noise(text))
    ]


def parse_declaration_entries(text: str, filename: str) -> list[dict]:
    """取函数声明及其**完整签名**（不是只有名字）。"""
    clean = strip_noise(text)
    entries = []
    for match in _DECLARATION_RE.finditer(clean):
        signature = " ".join(clean[match.start() : match.end()].split()).rstrip(";")
        entries.append(
            {
                "name": match.group(1),
                "kind": "函数",
                "text": f"{signature};",
                "file": filename,
                "line": _line_of(clean, match.start(1)),
            }
        )
    return entries


def parse_type_entries(text: str, filename: str) -> list[dict]:
    """取结构体、联合体、枚举与 typedef 的定义原文。

    名字取定义里最后出现的那个标识符（`typedef struct {...} Record;` 取 `Record`），
    取不到就跳过——认不出名字的类型对背景信息没有用。
    """
    clean = strip_noise(text)
    entries = []
    for match in _TYPE_RE.finditer(clean):
        statement = " ".join(match.group(0).split())
        names = re.findall(r"[A-Za-z_]\w*", statement)
        if not names:
            continue
        entries.append(
            {
                "name": names[-1],
                "kind": "类型",
                "text": statement[:400],
                "file": filename,
                "line": _line_of(clean, match.start(0)),
            }
        )
    return entries


def read_and_parse(files: list[Path], root: Path) -> tuple[list[dict], list[dict]]:
    """读一组源文件，返回（函数定义清单，全局变量清单），文件顺序即输入顺序。"""
    functions: list[dict] = []
    globals_: list[dict] = []
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        name = str(path.relative_to(root))
        functions.extend(parse_functions(text, name))
        globals_.extend(parse_globals(text, name))
    return functions, globals_
