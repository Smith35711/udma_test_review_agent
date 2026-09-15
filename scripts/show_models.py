"""打印模型配置总览：可选模型、当前生效项及其来源、密钥与网络出口状态。

用法:
  agent/.venv/bin/python agent2/scripts/show_models.py [--model NAME]

只读取配置，不发起任何请求，也不打印密钥取值。
退出码: 0 正常；1 模型配置非法，或生效项不在模型列表中。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent2.model_config import describe_models


def _proxy_cell(item: object) -> str:
    """PROXY 列取值：显示代理 URL 主机部分，无则 `直连`。

    只取主机与端口，去掉可能出现在 URL 里的凭据（`http://user:pass@host:port`）——
    这张表会打印到终端与 CI 日志里，凭据不应随表格外泄。同理，本列永远不含密钥。
    """
    proxy = getattr(item, "proxy", "") or ""
    if not proxy:
        return "直连"
    stripped = re.sub(r"^[a-zA-Z0-9+.-]+://", "", proxy)
    if "@" in stripped:
        stripped = stripped.rsplit("@", 1)[1]
    return stripped.rstrip("/") or "直连"


def main() -> int:
    parser = argparse.ArgumentParser(description="打印模型配置总览（不含密钥取值）")
    parser.add_argument(
        "--model",
        default="",
        help="模型配置名称（config/models.yaml 中的 name），留空取 active",
    )
    args = parser.parse_args()

    try:
        overview = describe_models(profile=args.model or None)
    except ValueError as exc:  # ModelConfigError 继承 ValueError
        print(f"模型配置错误: {exc}", file=sys.stderr)
        return 1

    print(f"模型配置: {overview.path}")
    print(f"生效模型: {overview.effective}（来源: {overview.source}；配置 active={overview.active}）")
    names = {item.name for item in overview.items}
    unknown = overview.effective not in names
    if unknown:
        print(
            f"模型配置错误: 生效模型 {overview.effective} 不在模型列表中，可选: {sorted(names)}",
            file=sys.stderr,
        )
    print()
    name_w = max([len(item.name) for item in overview.items] + [4])
    url_w = max([len(item.base_url) for item in overview.items] + [8])
    proxy_w = max([len(_proxy_cell(item)) for item in overview.items] + [7])
    print(
        f"{'NAME':<{name_w}}  {'MODEL':<26}  {'BASE_URL':<{url_w}}  {'ACTIVE':<6}  "
        f"{'PROXY':<{proxy_w}}  KEY"
    )
    for item in overview.items:
        # * 为本次生效项，- 为配置 active 但被 --model / LLM_PROFILE 覆盖
        mark = "*" if item.name == overview.effective else ("-" if item.active else "")
        print(
            f"{item.name:<{name_w}}  {item.model:<26}  {item.base_url:<{url_w}}  {mark:<6}  "
            f"{_proxy_cell(item):<{proxy_w}}  {'已配置' if item.has_api_key else '未配置'}"
        )
    # 网络出口有非默认设置的模型逐条展开：表格里的 PROXY 列只看得到代理，
    # 看不到「关掉了证书校验」这类同样会改变行为、却不会显示在 URL 里的设置。
    custom = [item for item in overview.items if item.network_custom]
    if custom:
        print()
        print("网络出口（非默认设置，改动了 SDK 默认行为的模型）:")
        for item in custom:
            print(f"  {item.name:<{name_w}}  {item.network}")
    print()
    print(
        f"共 {len(overview.items)} 个模型；用 --model NAME 或环境变量 LLM_PROFILE 切换"
        "（填模型 name，不是 provider 名）。\n"
        "优先级：--model > LLM_PROFILE > active。"
    )
    return 1 if unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
