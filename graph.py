from __future__ import annotations

import time
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from openai import RateLimitError

from agent_tools import build_tools, execute_action
from config import Settings
from stores import clear_pending_action, get_pending_action, get_user_memory, update_user_memory


SYSTEM_PROMPT = """
You are GraphDev, a conversational Discord development assistant.

You are not a rigid command bot. Users may speak naturally and refer to "this
project", "that file", "run it again", or "the previous folder". Use memory,
recent paths, and tools to resolve context. Ask one concise clarifying question
only when the next safe action is genuinely ambiguous.

Architecture and tool rules:
- Use tools to inspect, plan, read, create, modify, execute, install, and recover.
- All workspace operations are constrained to F:\\Upwork by the tools.
- Never ask the user to paste secrets. Never reveal .env contents.
- Prefer inspecting with scan_project_tool and read_file_tool before modifying.
- Risky operations are queued for approval by their tools. When a tool returns
  approval_required, clearly summarize what will happen and tell the user to
  reply YES or NO.
- Do not ask "Should I proceed?" for a write/edit/delete/run/install operation
  unless you have already called the matching tool and received approval_required.
  A plain natural-language confirmation is not enough; the pending action must
  be stored by a tool.
- Do not invent output artifacts. For .png, .pkl, .h5, .csv, model files, plots,
  reports, or notebook outputs, write executable code/notebook cells and use
  run_file_tool or run_notebook_tool so outputs are produced by real execution.
- Before replacing code, read the current file and preserve unrelated behavior.
- For existing files, use modify_file_tool. For new files, use write_file_tool.
- For large project builds, generate a plan, create files, then suggest running
  or installing dependencies as a separate approval-backed step.

Keep Discord responses practical and compact. Mention changed paths and whether
approval, execution, or a follow-up is needed.
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
            result = graph.invoke(state, config={"recursion_limit": 20})
            return result.get("final_response") or "I could not generate a response."
        except RateLimitError:
            return (
                "OpenAI rate-limited this request because it was too large or too many requests were sent at once. "
                "Please try again in a few seconds. If it keeps happening, ask me to work in smaller steps, such as "
                "`inspect the dataset first`, then `create the notebook`, then `run it`."
            )

    def _build_graph(self, user_id: str, attachments: list[dict[str, Any]], original_request: str = ""):
        tools = build_tools(self.settings, user_id, attachments, original_request)
        llm = ChatOpenAI(
            model=self.settings.openai_model,
            api_key=self.settings.openai_api_key,
            temperature=0.2,
        ).bind_tools(tools)

        def load_context(state: AgentState) -> AgentState:
            return {
                "memory": get_user_memory(state["user_id"]),
                "pending_action": get_pending_action(state["user_id"]),
            }

        def route_after_context(state: AgentState) -> Literal["approval", "agent"]:
            if state.get("pending_action"):
                return "approval"
            return "agent"

        def approval_node(state: AgentState) -> AgentState:
            pending = state.get("pending_action")
            decision = state.get("user_text", "").strip().upper()

            if not pending:
                return {"final_response": "I do not have a pending action to approve. Send me the task you want me to work on."}
            if decision == "NO":
                clear_pending_action(state["user_id"])
                return {"final_response": "Cancelled the pending action."}
            if decision != "YES":
                return {"final_response": "You have a pending action. Reply `YES` to approve it or `NO` to cancel it before starting another task."}

            try:
                result = execute_action(self.settings, state["user_id"], pending["action"], pending.get("args", {}))
                clear_pending_action(state["user_id"])
                response = f"Approved and completed `{pending['action']}`.\n\n```json\n{result}\n```"
                self._remember_turn(state["user_id"], "YES", response)
                if pending.get("continue_after_approval"):
                    request = pending.get("original_request") or state["user_text"]
                    return {
                        "user_text": request,
                        "messages": [
                            SystemMessage(
                                content=(
                                    f"The user approved and this action completed successfully: {pending['action']}.\n"
                                    f"Result:\n{result}\n\n"
                                    "Continue the original request from the next unfinished step. "
                                    "If another risky operation is needed, call the appropriate tool so it queues approval."
                                )
                            )
                        ],
                    }
                return {"final_response": response}
            except Exception as error:
                clear_pending_action(state["user_id"])
                return {
                    "final_response": (
                        f"I tried to run the approved `{pending.get('action')}` action, but it failed:\n"
                        f"`{error}`\n\nTell me to repair it and rerun if you want me to investigate."
                    )
                }

        def agent_node(state: AgentState) -> AgentState:
            attachments_summary = [
                {
                    "filename": item.get("filename"),
                    "content_type": item.get("content_type"),
                    "size": item.get("size"),
                }
                for item in state.get("attachments", [])
            ]
            context = (
                f"User memory: {state.get('memory', {})}\n"
                f"Current Discord attachments: {attachments_summary}\n"
                f"Allowed workspace root: {self.settings.workspace_root}\n"
                "The user's latest message follows."
            )
            response = self._invoke_llm_with_retry(
                llm,
                [
                    SystemMessage(content=SYSTEM_PROMPT),
                    SystemMessage(content=context),
                    *state["messages"],
                    HumanMessage(content=state["user_text"]),
                ],
            )
            return {"messages": [response]}

        def final_node(state: AgentState) -> AgentState:
            for message in reversed(state.get("messages", [])):
                if isinstance(message, AIMessage):
                    content = str(message.content)
                    self._remember_turn(state["user_id"], state["user_text"], content)
                    return {"final_response": content}
            return {"final_response": "Done."}

        graph = StateGraph(AgentState)
        graph.add_node("load_context", load_context)
        graph.add_node("approval", approval_node)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", ToolNode(tools))
        graph.add_node("final", final_node)
        graph.add_edge(START, "load_context")
        graph.add_conditional_edges("load_context", route_after_context)
        graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: "final"})
        graph.add_edge("tools", "agent")
        graph.add_conditional_edges(
            "approval",
            lambda state: END if state.get("final_response") else "agent",
            {END: END, "agent": "agent"},
        )
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
