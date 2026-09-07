from __future__ import annotations

import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.graph import END, START, StateGraph

from udma_test_review_agent.llm import create_llm, generate_text


class ChatState(TypedDict):
    input_text: str
    output_text: str


def llm_node(state: ChatState) -> dict[str, str]:
    llm = create_llm()
    reply = generate_text(llm, state["input_text"])
    return {"output_text": reply}


def build_graph():
    builder = StateGraph(ChatState)
    builder.add_node("llm", llm_node)
    builder.add_edge(START, "llm")
    builder.add_edge("llm", END)
    return builder.compile()


def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "请用一句话介绍什么是智能体。"
    graph = build_graph()
    result = graph.invoke({"input_text": question})
    print("=" * 40)
    print(result["output_text"])
    print("=" * 40)


if __name__ == "__main__":
    main()
