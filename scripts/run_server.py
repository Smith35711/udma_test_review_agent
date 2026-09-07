"""启动评审智能体 Web 服务。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402


def main() -> None:
    uvicorn.run("udma_test_review_agent.server:app", host="127.0.0.1", port=8765)


if __name__ == "__main__":
    main()
