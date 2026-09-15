"""生成大模块样例：一个约 2500 行的 C 模块，用于验证大模块分支。

刻意包含：

- **循环调用组**：一组两元相互递归（`bm_parse_a` ⇄ `bm_parse_b`）与一组三元环
  （`bm_scan_a → bm_scan_b → bm_scan_c → bm_scan_a`），用来验证「调用组不可拆」；
- **扇出枢纽**：`bm_dispatch` 调用十几个下游处理函数，用来验证形态识别；
- **重复代码**：五个几乎相同的 `bm_load_*`，只有缓冲区大小与字段名不同；
- **植入缺陷**：越界拷贝、未判空的分配、句柄泄漏、双重释放、释放后使用、
  无边界 sprintf、无终止符的 strncpy、整数溢出 —— 用来验证分块之后检视质量
  没有被稀释。

输出确定（不使用随机数），可重复生成。生成完会打印植入缺陷的行号，
并写一份 `EXPECTED.md` 到同目录。

用法：
    PYTHONPATH=agent2/src agent/.venv/bin/python agent2/eval/make_large_fixture.py
"""

from __future__ import annotations

from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parents[2] / "test_project_large"
HANDLER_COUNT = 13
FILLER_PER_HANDLER = 170
"""每个处理函数的填充行数。调这个数就能把总规模推到阈值之上——大模块分支要求
模块超过 2000 行才会触发，样例必须真的越线，否则测的是小模块路径。"""


class _Source:
    """逐行累积源码，同时记录行号——植入缺陷的位置要靠它报出来。"""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.defects: list[tuple[int, str]] = []

    @property
    def next_line(self) -> int:
        return len(self.lines) + 1

    def add(self, *text: str) -> None:
        self.lines.extend(text)

    def defect(self, note: str) -> None:
        """把**下一行**记为一个植入缺陷。"""
        self.defects.append((self.next_line, note))

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _filler(source: _Source, index: int, count: int) -> None:
    """填一些看着像真的、但没有缺陷的语句，把函数撑到需要的规模。"""
    source.add(
        "    int total = 0;",
        "    int i;",
        "",
        f"    for (i = 0; i < {count}; i++) {{",
    )
    for step in range(count // 6):
        source.add(
            f"        total += (i * {step + 2}) ^ {index + 1};",
            f"        total -= (total >> {step % 5 + 1});",
            f"        total ^= (total << {step % 4 + 1});",
            "        if (total < 0) {",
            "            total = -total;",
            "        }",
        )
    source.add("    }", "    return total;", "}", "")


def build_module() -> _Source:
    source = _Source()
    source.add(
        '#include "bigmod.h"',
        "",
        "#include <stdio.h>",
        "#include <stdlib.h>",
        "#include <string.h>",
        "",
        "static char g_state[64];",
        "static int g_counter = 0;",
        "",
    )

    # ---- 初始化：strncpy 不保证终止符 ----
    source.add("int bm_init(const char *name) {")
    source.add(
        "    int i;",
        "",
        "    if (name == NULL) {",
        "        return -1;",
        "    }",
    )
    source.defect("strncpy 用 sizeof(g_state) 填满 64 字节且不补终止符，g_state 之后不再是合法字符串")
    source.add("    strncpy(g_state, name, sizeof(g_state));")
    source.add("    for (i = 0; i < 8; i++) {", "        g_counter += i;", "    }", "    return g_counter;", "}", "")

    # ---- 五处几乎相同的加载函数：重复代码 ----
    for index in range(5):
        size = 64 + index * 32
        source.add(f"int bm_load_{index}(const char *src) {{")
        source.add(f"    char buf[{size}];", "    int length = 0;", "")
        if index == 2:
            source.defect("strcpy 无边界拷贝，src 超过 128 字节即越界写")
        source.add("    strcpy(buf, src);", "    length = (int)strlen(buf);", "    g_counter += length;")
        source.add(
            "",
            f"    if (length > {size - 1}) {{",
            "        return -1;",
            "    }",
            f"    return length + {index};",
            "}",
            "",
        )

    # ---- 扇出枢纽 + 被它调用的处理函数 ----
    source.add("int bm_dispatch(int opcode, const char *payload) {")
    source.add("    switch (opcode) {")
    for index in range(HANDLER_COUNT):
        source.add(f"    case {index}:", f"        return bm_handle_{index:02d}(payload);")
    source.add("    default:", "        break;", "    }", "    return -1;", "}", "")

    for index in range(HANDLER_COUNT):
        source.add(f"int bm_handle_{index:02d}(const char *payload) {{")
        if index == 3:
            source.defect("malloc 未判空即解引用 p[0]，分配失败时解引用空指针")
            source.add("    char *p = malloc(strlen(payload) + 1);", "    p[0] = '\\0';")
        if index == 7:
            source.defect("malloc 得到的缓冲未初始化即读取，buf[0] 是未定义值")
            source.add("    char *buf = malloc(256);", "    int first = buf[0];", "    (void)first;")
        _filler(source, index, FILLER_PER_HANDLER)

    # ---- 两元相互递归：bm_parse_a ⇄ bm_parse_b ----
    for name, other in (("a", "b"), ("b", "a")):
        source.add(f"int bm_parse_{name}(const char *text, int depth) {{")
        source.add("    char scratch[32];", "")
        if name == "a":
            source.defect("sprintf 无边界限制写入 dst，长度不可控")
            source.add('    sprintf(scratch, "%s", text);')
        source.add(
            "    if (depth <= 0) {",
            "        return 0;",
            "    }",
            f"    if (text[0] == '{name}') {{",
            f"        return bm_parse_{other}(text + 1, depth - 1);",
            "    }",
            "    return depth;",
            "}",
            "",
        )

    # ---- 三元环：bm_scan_a → bm_scan_b → bm_scan_c → bm_scan_a ----
    for name, following in (("a", "b"), ("b", "c"), ("c", "a")):
        source.add(f"int bm_scan_{name}(int value, int rounds) {{")
        if name == "b":
            source.defect("count * sizeof(int) 未检查乘法溢出，size_t 为 32 位时分配过小")
            source.add("    unsigned count = (unsigned)value;", "    int *table = malloc(count * sizeof(int));")
            source.add("    if (table == NULL) {", "        return -1;", "    }")
        source.add(
            "    if (rounds <= 0) {",
            "        return value;",
            "    }",
            f"    return bm_scan_{following}(value + 1, rounds - 1);",
            "}",
            "",
        )

    # ---- 句柄泄漏 ----
    source.add("int bm_flush(const char *path) {")
    source.add('    FILE *fp = fopen(path, "w");', "", "    if (fp == NULL) {", "        return -1;", "    }")
    source.add('    fprintf(fp, "%s", g_state);', "    fflush(fp);", "    return 0;", "}", "")
    source.defects.append((source.next_line - 4, "fopen 得到的句柄在所有路径上都没有 fclose，每次调用泄漏一个"))

    # ---- 释放后使用 + 双重释放 ----
    source.add("int bm_report(int size) {")
    source.add("    char *block = malloc(size);", "", "    if (block == NULL) {", "        return -1;", "    }")
    source.add('    strncpy(block, g_state, size);', "    free(block);")
    source.defect("free 之后仍读取 block[0]，释放后使用")
    source.add("    return block[0];", "}", "")

    source.add("void bm_close(void) {")
    source.add("    char *p = malloc(16);", "", "    if (p == NULL) {", "        return;", "    }")
    source.defect("同一指针连续 free 两次，double free")
    source.add("    free(p);", "    free(p);", "}", "")

    source.add("void bm_cleanup(void) {", "    g_counter = 0;", "    memset(g_state, 0, sizeof(g_state));", "}", "")
    return source


CLIENT_C = """#include "client.h"

#include <string.h>

#include "bigmod.h"

static bm_record_t g_record;

int client_begin(const char *name) {
    int length;

    memset(&g_record, 0, sizeof(g_record));
    length = (int)strlen(name);
    if (length >= BM_MAX_NAME) {
        length = BM_CLAMP(length, 0, BM_MAX_NAME - 1);
    }
    memcpy(g_record.name, name, length);
    g_record.id = 1;
    g_record.flags = 0;
    return bm_init(name);
}

int client_submit(int opcode, const char *payload) {
    char buffer[BM_MAX_NAME];

    strncpy(buffer, payload, sizeof(buffer));
    return bm_dispatch(opcode, buffer);
}

int client_finish(const char *path) {
    int status = bm_flush(path);

    if (status != BM_OK) {
        return -1;
    }
    bm_close();
    return 0;
}
"""

CLIENT_H = """#ifndef CLIENT_H
#define CLIENT_H

int client_begin(const char *name);
int client_submit(int opcode, const char *payload);
int client_finish(const char *path);

#endif
"""


def build_header() -> str:
    names = ["int bm_init(const char *name);", "void bm_close(void);", "void bm_cleanup(void);",
             "int bm_dispatch(int opcode, const char *payload);", "int bm_flush(const char *path);",
             "int bm_report(int size);"]
    names += [f"int bm_load_{index}(const char *src);" for index in range(5)]
    names += [f"int bm_handle_{index:02d}(const char *payload);" for index in range(HANDLER_COUNT)]
    names += [f"int bm_parse_{name}(const char *text, int depth);" for name in ("a", "b")]
    names += [f"int bm_scan_{name}(int value, int rounds);" for name in ("a", "b", "c")]
    body = "\n".join(sorted(names))
    # 宏、宏函数与类型定义：既是被本模块使用的，也是**别的模块依赖本模块**时要看的东西
    preamble = """#define BM_MAX_NAME 32
#define BM_VERSION "1.4"
#define BM_CLAMP(v, lo, hi) ((v) < (lo) ? (lo) : ((v) > (hi) ? (hi) : (v)))

typedef struct {
    int id;
    char name[BM_MAX_NAME];
    int flags;
} bm_record_t;

typedef enum {
    BM_OK = 0,
    BM_ERR = -1
} bm_status_t;
"""
    return f"#ifndef BIGMOD_H\n#define BIGMOD_H\n\n{preamble}\n{body}\n\n#endif\n"


def main() -> None:
    source = build_module()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    code_path = OUTPUT_DIR / "bigmod.c"
    code_path.write_text(source.text(), encoding="utf-8")
    (OUTPUT_DIR / "bigmod.h").write_text(build_header(), encoding="utf-8")
    # 另加一个**依赖 bigmod** 的小模块：它让「模块间依赖背景信息」与「小模块先并行跑、
    # 大模块成批跑」这两条都有真东西可验，而不是只有一个自给自足的大模块
    (OUTPUT_DIR / "client.c").write_text(CLIENT_C, encoding="utf-8")
    (OUTPUT_DIR / "client.h").write_text(CLIENT_H, encoding="utf-8")

    lines = len(code_path.read_text(encoding="utf-8").splitlines())
    report = ["# test_project_large 的植入缺陷（由生成脚本输出，行号自动核对）", ""]
    report += [f"- bigmod.c:{line}  {note}" for line, note in sorted(source.defects)]
    (OUTPUT_DIR / "EXPECTED.md").write_text(
        "\n".join(report) + f"\n\n模块规模：{lines} 行（bigmod.c + bigmod.h）\n", encoding="utf-8"
    )

    print(f"已生成 {code_path}：{lines} 行")
    print("植入缺陷：")
    for line, note in sorted(source.defects):
        print(f"  bigmod.c:{line}  {note}")


if __name__ == "__main__":
    main()
