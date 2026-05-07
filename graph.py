from __future__ import annotations

import json
import time
from typing import Annotated, Any, Literal, TypedDict  # Literal kept for route_after_agent

from anthropic import RateLimitError
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from agent_tools import build_tools
from config import Settings
from stores import get_pending_action, get_user_memory, update_user_memory


SYSTEM_PROMPT = """
You are GraphDev, a conversational assistant on Discord powered by Claude.

You have full access to the F:\\Upwork workspace. You can read, create, edit, move,
delete, and run any file or project inside it — code, notebooks, websites, Word docs,
spreadsheets, LaTeX, MATLAB, or any other file type. Speak naturally, not like a CLI tool.

## Approval model — one approval, then execute everything

For any task that involves writing, modifying, deleting, running, or installing:
1. First scan and read to understand what exists.
2. Write a short summary of exactly what you are about to do.
3. Ask the user once: "Shall I go ahead?" or similar.
4. After they confirm (any natural agreement: "yes", "go ahead", "sure", "do it",
   "yeah", "ok", "run it"), execute EVERYTHING in one go. No further interruptions.

Do NOT ask again mid-task. Do NOT ask separately for each file, step, or operation.

For clear, explicit single-step requests ("create file X", "delete folder Y",
"run that script", "summarize this file") — just do it immediately, no approval needed.

## File rules

- Word .docx: write_docx_tool (new) or modify_docx_tool (edit). Never write_file_tool for .docx.
- All other files: write_file_tool (new) or modify_file_tool (existing).
- Multi-file changes: use batch_modify_tool or batch_create_files_tool in one call.
- Never reveal .env or modify secret files.
- Never invent results — use run_file_tool or run_notebook_tool for real output.
- In-chat analysis ("summarize", "explain", "what does this do"): use analyze_file_tool,
  answer directly. Do not write a file unless the user asks to save a report.
- Saved reports (.md or .docx): generate_technical_report_tool.
- End-to-end ML pipeline (dataset → notebook → run → Word report): run_ml_pipeline_tool.
- Style-preserving Word edits (add project, match format): modify_docx_tool.

## Discord style

Concise responses. Mention changed file paths. One sentence on what's next. No filler.
""".strip()


class AgentState(TypedDict):
    user_id: str
    user_text: str
    attachments: list[dict[str, Any]]
    memory: dict[str, Any]
    pending_action: dict[str, Any] | None
    messages: Annotated[list[BaseMessage], add_messages]
    final_response: str


class GraphDevAgent:
    def __init__(self, settings: Settings):
        self.settings = settings

    def invoke(self, user_id: str, user_text: str, attachments: list[dict[str, Any]] | None = None) -> str:
        pending = get_pending_action(user_id)
        effective_attachments = attachments or []
        if not effective_attachments and pending:
            effective_attachments = pending.get("original_attachments", [])
        graph = self._build_graph(user_id, effective_attachments, user_text)
        state: AgentState = {
            "user_id": user_id,
            "user_text": user_text,
            "attachments": effective_attachments,
            "memory": get_user_memory(user_id),
            "pending_action": pending,
            "messages": [],
            "final_response": "",
        }
        try:
            result = graph.invoke(state, config={"recursion_limit": 35})
            return result.get("final_response") or "I could not generate a response."
        except RateLimitError:
            return (
                "Claude rate-limited this request. Please try again in a moment. "
                "For large tasks, break them into smaller steps: inspect first, then create, then run."
            )
        except Exception as error:
            if "Recursion limit" in str(error) or "GRAPH_RECURSION_LIMIT" in str(error):
                update_user_memory(
                    user_id,
                    last_failed_task={"user_text": user_text, "pending_action": pending, "error": str(error)},
                )
                return (
                    "I got stuck in a tool-calling loop. Please retry the request. "
                    "If it keeps happening, break the task into smaller steps."
                )
            update_user_memory(
                user_id,
                last_failed_task={"user_text": user_text, "pending_action": pending, "error": str(error)},
            )
            raise

    def _build_graph(self, user_id: str, attachments: list[dict[str, Any]], original_request: str = ""):
        tools = build_tools(self.settings, user_id, attachments, original_request)
        llm = ChatAnthropic(
            model=self.settings.claude_model,
            api_key=self.settings.anthropic_api_key,
            temperature=0.2,
            max_tokens=8192,
        ).bind_tools(tools)

        def load_context(state: AgentState) -> AgentState:
            return {
                "memory": get_user_memory(state["user_id"]),
                "pending_action": get_pending_action(state["user_id"]),
            }

        def agent_node(state: AgentState) -> AgentState:
            memory = state.get("memory", {})
            pending = state.get("pending_action")
            attachments_summary = [
                {"filename": a.get("filename"), "content_type": a.get("content_type"), "size": a.get("size")}
                for a in state.get("attachments", [])
            ]
            context_parts = [
                f"Workspace root: {self.settings.workspace_root}",
                f"User memory: {memory}",
                f"Pending approval action: {json.dumps(pending) if pending else 'none'}",
                f"Discord attachments in this message: {attachments_summary}",
            ]
            response = self._invoke_llm_with_retry(
                llm,
                [
                    SystemMessage(content=SYSTEM_PROMPT),
                    SystemMessage(content="\n".join(context_parts)),
                    *state["messages"],
                    HumanMessage(content=state["user_text"]),
                ],
            )
            return {"messages": [response]}

        def route_after_agent(state: AgentState) -> Literal["tools", "final"]:
            last = state["messages"][-1] if state["messages"] else None
            if isinstance(last, AIMessage) and last.tool_calls:
                return "tools"
            return "final"

        def final_node(state: AgentState) -> AgentState:
            for msg in reversed(state["messages"]):
                if isinstance(msg, AIMessage) and str(msg.content).strip():
                    content = str(msg.content)
                    self._remember_turn(state["user_id"], state["user_text"], content)
                    return {"final_response": content}
                if isinstance(msg, ToolMessage):
                    content = str(msg.content)
                    self._remember_turn(state["user_id"], state["user_text"], content)
                    return {"final_response": content}
            return {"final_response": "Done."}

        graph = StateGraph(AgentState)
        graph.add_node("load_context", load_context)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", ToolNode(tools))
        graph.add_node("final", final_node)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "agent")
        graph.add_conditional_edges("agent", route_after_agent)
        graph.add_edge("tools", "agent")
        graph.add_edge("final", END)
        return graph.compile()

    @staticmethod
    def _remember_turn(user_id: str, user_text: str, assistant_text: str) -> None:
        memory = get_user_memory(user_id)
        summary = memory.get("conversation_summary", "")
        new_line = f"User: {user_text[:300]}\nAssistant: {assistant_text[:500]}"
        update_user_memory(user_id, conversation_summary=f"{summary}\n{new_line}".strip()[-3000:])

    @staticmethod
    def _invoke_llm_with_retry(llm, messages: list[BaseMessage]) -> AIMessage:
        waits = [0.5, 1.5, 3.0]
        for attempt, wait_seconds in enumerate(waits, start=1):
            try:
                return llm.invoke(messages)
            except RateLimitError:
                if attempt == len(waits):
                    raise
                time.sleep(wait_seconds)
        return llm.invoke(messages)



def build_graphdev_agent(settings: Settings) -> GraphDevAgent:
    return GraphDevAgent(settings)
