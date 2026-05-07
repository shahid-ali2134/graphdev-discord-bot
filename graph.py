from __future__ import annotations

import json
import re
import time
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from openai import RateLimitError

from agent_tools import build_tools, execute_action
from config import Settings
from stores import clear_pending_action, get_pending_action, get_user_memory, set_pending_action, update_user_memory


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
- For analysis requests that ask for an answer "here", "in chat", "summary", "explain", or to check a condition and respond, use analyze_file_tool and answer in Discord. Do not create a report file unless the user explicitly asks to create/save/export/write a report/document/file. Use generate_technical_report_tool only when a saved Markdown or Word report file is requested. This applies to notebooks, source files, HTML/CSS, PDFs, DOCX, PPTX, spreadsheets, LaTeX, and other readable files.
- When generating technical reports, use a formal, professional style with no first-person language. Do not use casual phrasing.
- The 12-section technical report outline is a reference structure, not a rigid requirement. Adapt headings and depth to the file type, user request, and available evidence. Keep only sections that add value, and combine or shorten irrelevant sections.
- When the user gives a condition in the prompt, the report must explicitly check for that condition, list whether matching evidence was found, and include relevant excerpts or detected signals.
- Never invent metrics, model names, dataset names, results, plots, tables, conclusions, methodology details, or claims. If information is missing, say "Not explicitly available in the provided file." or "No evidence of this was found in the analyzed content."
- For Word `.docx` files, use write_docx_tool to create real Word documents and modify_docx_tool to edit existing Word documents. Do not use write_file_tool to create `.docx` files. Default DOCX formatting is Times New Roman; title 28pt black centered; normal text 11pt black; Heading 1 16pt bold black; Heading 2 14pt bold black unless the prompt explicitly says otherwise.
- For existing non-DOCX files, use modify_file_tool. For new non-DOCX files, use write_file_tool.
- For large project builds, generate a plan, create files, then suggest running
  or installing dependencies as a separate approval-backed step.

Keep Discord responses practical and compact. Mention changed paths and whether
approval, execution, or a follow-up is needed.
""".strip()




def is_file_analysis_request(text: str) -> bool:
    lowered = text.lower().strip()
    lowered = re.sub(r"^<@!?\d+>\s*", "", lowered)
    lowered = re.sub(r"^@\S+\s*", "", lowered)
    file_terms = [
        "file", "folder", "directory", "html", "css", "javascript", "python", "notebook",
        "pdf", "ppt", "pptx", "doc", "docx", "tex", "spreadsheet", "csv", "xlsx",
        ".py", ".html", ".css", ".js", ".ts", ".cpp", ".c", ".h", ".java",
        ".pdf", ".pptx", ".docx", ".tex", ".ipynb", "f:\\", "/testing graphdev",
    ]
    analysis_terms = [
        "summary", "summarize", "technical summary", "analyze", "analyse", "what is happening",
        "what's happening", "whats happening", "explain", "describe", "what does", "what is in",
        "what's in", "whats in", "happening in", "that file", "this file",
    ]
    return any(term in lowered for term in file_terms) and any(term in lowered for term in analysis_terms)
def is_context_question(text: str) -> bool:
    lowered = text.lower().strip()
    if approval_decision(lowered):
        return False
    if is_file_analysis_request(lowered):
        return False
    context_markers = [
        "remember", "last task", "previous task", "failed", "what happened", "pending",
        "status", "context", "what were", "what was", "what is", "whats", "what's",
        "which task", "what did", "task", "resume", "working on", "were working",
        "continue what", "recall", "history", "summarize", "summary",
    ]
    action_starters = [
        "create", "write", "modify", "delete", "run", "install", "save", "generate",
        "apply", "edit", "replace", "add", "remove", "execute", "approve",
    ]
    if any(lowered.startswith(starter) for starter in action_starters):
        return False
    return any(marker in lowered for marker in context_markers)

def approval_decision(text: str) -> str | None:
    lowered = text.lower().strip()
    lowered = re.sub(r"^<@!?\d+>\s*", "", lowered)
    lowered = re.sub(r"^@\S+\s*", "", lowered)
    if not lowered:
        return None
    first = lowered.split()[0].strip("`.,!?:;()[]{}")
    if first in {"yes", "y", "approve", "approved", "proceed", "continue", "ok", "okay", "run"}:
        return "YES"
    if first in {"no", "n", "cancel", "stop", "reject", "deny"}:
        return "NO"
    return None

def format_tool_response_for_discord(content: str) -> str:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content

    if payload.get("status") == "approval_required":
        action = payload.get("action", "action")
        reason = payload.get("reason", "This needs your approval.")
        args = payload.get("arguments", {}) or {}
        path = args.get("path") or args.get("project") or args.get("destination") or args.get("backup_path")
        report_path = args.get("report_path")
        overwrite = args.get("overwrite")
        content_note = args.get("content") or args.get("new_content") or args.get("files") or args.get("attachments")
        chat_summary = str(payload.get("chat_summary") or "").strip()

        lines = []
        if chat_summary:
            lines.extend(["Here is the chat summary:", "", chat_summary, ""])
        lines.extend([f"I need your approval before I run `{action}`.", "", f"Reason: {reason}"])
        if path:
            lines.append(f"Target: `{path}`")
        if report_path:
            lines.append(f"Output: `{report_path}`")
        if overwrite is not None:
            lines.append(f"Overwrite existing file: `{overwrite}`")
        if content_note:
            lines.append(f"Queued content: {content_note}")
        lines.append("")
        lines.append("Reply `YES` to approve or `NO` to cancel.")
        return "\n".join(lines)

    if payload.get("status") == "plan_ready":
        return "I have prepared the plan. Tell me what part you want me to apply, and I will queue the required approval-backed action."

    return content

def _clean_user_request(value: Any) -> str:
    text = str(value or "").strip()
    if approval_decision(text):
        return ""
    return text

def _summarize_action(action: str | None) -> str:
    labels = {
        "generate_technical_report": "generate a technical report",
        "write_docx": "create a Word document",
        "modify_docx": "modify a Word document",
        "write_file": "write a file",
        "modify_file": "modify a file",
        "create_project": "create a project",
        "delete_path": "delete a path",
        "run_file": "run a Python file",
        "run_notebook": "run a notebook",
        "install_dependencies": "install dependencies",
        "save_attachment": "save attachments",
        "rollback": "roll back changes",
    }
    return labels.get(str(action or ""), str(action or "complete an action").replace("_", " "))

def _summarize_pending_action(pending: dict[str, Any]) -> str:
    action = pending.get("action")
    args = pending.get("args", {}) or {}
    original_request = _clean_user_request(pending.get("original_request"))
    lines = [f"We were working on a request to {_summarize_action(action)}."]

    if original_request:
        lines.append(f"Original request: {original_request}")

    path = args.get("path") or args.get("project") or args.get("destination") or args.get("backup_path")
    report_path = args.get("report_path")
    if path:
        lines.append(f"Source/target: `{path}`")
    if report_path:
        lines.append(f"Output file: `{report_path}`")

    instructions = str(args.get("instructions") or "").strip()
    if instructions:
        compact = re.sub(r"\s+", " ", instructions)
        if len(compact) > 500:
            compact = f"{compact[:497].rstrip()}..."
        lines.append(f"Instructions remembered: {compact}")

    reason = str(pending.get("reason") or "").strip()
    if reason:
        lines.append(f"Why it is waiting: {reason}")

    lines.append("It is still waiting for approval. Reply `YES` to approve it or `NO` to cancel it.")
    return "\n".join(lines)

def _summarize_failed_task(failed: dict[str, Any]) -> str:
    error = str(failed.get("error") or "").strip()
    request = _clean_user_request(failed.get("user_text"))
    pending = failed.get("pending_action") if isinstance(failed.get("pending_action"), dict) else None

    if "queue_action() got an unexpected keyword argument 'continue_after_approval'" in error:
        if pending:
            return (
                "An earlier approval handoff failed because of an internal bug that has been fixed locally. "
                "The task itself is still recoverable from the pending action below."
            )
        return "An earlier approval handoff failed because of an internal bug that has been fixed locally."

    if "Recursion limit" in error or "GRAPH_RECURSION_LIMIT" in error:
        base = "The last attempt got stuck in a tool-calling loop before finishing."
    elif error:
        base = "The last attempt failed before it completed."
    else:
        base = "The last task did not finish."

    if request:
        base += f"\nRequest: {request}"
    if pending:
        base += f"\nRelated task: {_summarize_action(pending.get('action'))}."
    return base

def _summarize_memory_context(memory: dict[str, Any]) -> str:
    active = memory.get("active_project") or memory.get("active_folder")
    recent_paths = memory.get("recent_paths") or []
    lines = []
    if active:
        lines.append(f"The active project I remember is `{active}`.")
    if recent_paths:
        visible = ", ".join(f"`{item}`" for item in recent_paths[:5])
        lines.append(f"Recent files or folders: {visible}.")
    if not lines:
        lines.append("I do not have a pending approval, failed task, or active project recorded right now.")
    return "\n".join(lines)

def _looks_like_pending_update(text: str) -> bool:
    lowered = re.sub(r"^<@!?\d+>\s*", "", text.lower().strip())
    lowered = re.sub(r"^@\S+\s*", "", lowered)
    if not lowered or approval_decision(lowered) or is_context_question(lowered):
        return False
    update_markers = [
        "should be", "make it", "change", "instead", "rather", "use", "output", "file should",
        "save as", "name it", "rename", "format", "font", "include", "exclude", "add", "remove",
    ]
    return any(marker in lowered for marker in update_markers)

def _with_docx_report_path(report_path: str, source_path: str) -> str:
    chosen = report_path or source_path
    if re.search(r"\.(doc|docx)$", chosen, flags=re.IGNORECASE):
        return re.sub(r"\.doc$", ".docx", chosen, flags=re.IGNORECASE)
    if "." in chosen.split("\\")[-1] or "." in chosen.split("/")[-1]:
        return re.sub(r"\.[^.\\/]+$", ".docx", chosen)
    return f"{chosen}.docx"

def _fallback_pending_update(pending: dict[str, Any], user_text: str) -> tuple[dict[str, Any], str] | None:
    lowered = user_text.lower()
    if "word" not in lowered and ".doc" not in lowered and "docx" not in lowered:
        return None

    updated = json.loads(json.dumps(pending))
    args = updated.setdefault("args", {})
    args["report_path"] = _with_docx_report_path(str(args.get("report_path") or ""), str(args.get("path") or "report"))
    instructions = str(args.get("instructions") or "").strip()
    docx_instruction = "Create the output as a real Word .docx file using Times New Roman styling unless another font is explicitly requested."
    if docx_instruction not in instructions:
        args["instructions"] = f"{instructions}\n{docx_instruction}".strip()
    updated["original_request"] = user_text
    updated["continue_after_approval"] = False
    return updated, f"Got it. I updated the pending report so the output will be a Word file: `{args['report_path']}`. Reply `YES` to approve it or `NO` to cancel it."

def _format_completed_action(action: str, result: str) -> str:
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        payload = {}

    lines = [f"Approved and completed {_summarize_action(action)}."]
    path = payload.get("path") or payload.get("project") or payload.get("destination")
    backup = payload.get("backup")
    if path:
        lines.append(f"Output: `{path}`")
    if backup:
        lines.append(f"Backup: `{backup}`")
    if not path and not backup and result:
        compact = re.sub(r"\s+", " ", result).strip()
        if len(compact) > 500:
            compact = f"{compact[:497].rstrip()}..."
        lines.append(f"Result: {compact}")
    return "\n".join(lines)

class AgentState(TypedDict):
    user_id: str
    user_text: str
    attachments: list[dict[str, Any]]
    memory: dict[str, Any]
    pending_action: dict[str, Any] | None
    messages: Annotated[list[BaseMessage], add_messages]
    route_label: str
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
            final_response = result.get("final_response") or "I could not generate a response."
            return format_tool_response_for_discord(str(final_response))
        except RateLimitError:
            return (
                "OpenAI rate-limited this request because it was too large or too many requests were sent at once. "
                "Please try again in a few seconds. If it keeps happening, ask me to work in smaller steps, such as "
                "`inspect the dataset first`, then `create the notebook`, then `run it`."
            )
        except Exception as error:
            if "Recursion limit" in str(error) or "GRAPH_RECURSION_LIMIT" in str(error):
                failed = {
                    "user_text": user_text,
                    "pending_action": pending,
                    "error": str(error),
                }
                update_user_memory(user_id, last_failed_task=failed)
                return (
                    "I got stuck in a tool-calling loop before finishing. I tightened the workflow to stop after queued approvals. "
                    "Please retry the request. For notebook reports, ask me to generate the report from the notebook and approve the queued file write."
                )
            update_user_memory(user_id, last_failed_task={"user_text": user_text, "pending_action": pending, "error": str(error)})
            raise

    def _build_graph(self, user_id: str, attachments: list[dict[str, Any]], original_request: str = ""):
        tools = build_tools(self.settings, user_id, attachments, original_request)
        classifier_llm = ChatOpenAI(
            model=self.settings.openai_model,
            api_key=self.settings.openai_api_key,
            temperature=0,
        )
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

        def classify_node(state: AgentState) -> AgentState:
            pending = state.get("pending_action")
            user_text = state.get("user_text", "")
            fallback_decision = approval_decision(user_text)
            if fallback_decision in {"YES", "NO"}:
                return {"route_label": "approval"}
            if pending and _looks_like_pending_update(user_text):
                return {"route_label": "pending_update"}
            if is_context_question(user_text):
                return {"route_label": "status"}
            if not pending:
                return {"route_label": "agent"}

            prompt = (
                "Classify the user's latest Discord message for a conversational development assistant. "
                "Return only compact JSON with key route. Allowed routes: approval, status, pending_update, agent. "
                "Use approval only when the user is approving or cancelling a pending action. "
                "Use status when the user asks what the current task is, what was pending, what failed, what was being worked on, or asks to resume by first explaining context. "
                "Use pending_update when a pending action exists and the user corrects, refines, or changes that queued action instead of approving or cancelling it. "
                "Use agent when the user gives a new task or asks to perform work.\n\n"
                f"Pending action exists: {bool(pending)}\n"
                f"Pending action summary: {pending}\n"
                f"User message: {user_text}"
            )
            try:
                response = classifier_llm.invoke([SystemMessage(content=prompt)])
                payload = json.loads(str(response.content))
                route = payload.get("route", "agent")
            except Exception:
                if pending and is_context_question(user_text):
                    route = "status"
                elif pending:
                    route = "approval"
                else:
                    route = "agent"
            if route not in {"approval", "status", "pending_update", "agent"}:
                route = "agent"
            return {"route_label": route}
        def route_after_context(state: AgentState) -> Literal["approval", "status", "pending_update", "agent"]:
            route = state.get("route_label", "agent")
            if route in {"approval", "status", "pending_update", "agent"}:
                return route
            return "agent"


        def status_node(state: AgentState) -> AgentState:
            memory = state.get("memory", {}) or {}
            pending = state.get("pending_action")
            failed = memory.get("last_failed_task")
            parts = []
            if failed and isinstance(failed, dict):
                parts.append(_summarize_failed_task(failed))
            if pending:
                parts.append(_summarize_pending_action(pending))
            if not parts:
                parts.append(_summarize_memory_context(memory))
            response = "\n\n".join(parts)
            self._remember_turn(state["user_id"], state["user_text"], response)
            return {"final_response": response}
        def pending_update_node(state: AgentState) -> AgentState:
            pending = state.get("pending_action")
            user_text = state.get("user_text", "")
            if not pending:
                return {"final_response": "I do not have a pending action to update right now."}

            fallback = _fallback_pending_update(pending, user_text)
            if fallback:
                updated, reply = fallback
                set_pending_action(state["user_id"], updated)
                self._remember_turn(state["user_id"], user_text, reply)
                return {"final_response": reply}

            prompt = (
                "You update one queued approval action for a conversational Discord development assistant. "
                "The user is refining the pending action, not approving it. Return only JSON with keys updated_pending_action and reply. "
                "Preserve the pending action schema and only change fields requested by the user. "
                "Do not expose raw JSON in the reply. The reply should briefly summarize the updated queued task and ask for YES or NO. "
                "If the user asks for a Word/doc/docx output, set args.report_path to a .docx path. "
                "If the action is generate_technical_report, keep that action name; .docx output will be handled during execution.\n\n"
                f"Pending action JSON:\n{json.dumps(pending, ensure_ascii=False)}\n\n"
                f"User correction:\n{user_text}"
            )
            try:
                response = classifier_llm.invoke([SystemMessage(content=prompt)])
                payload = json.loads(str(response.content))
                updated = payload.get("updated_pending_action")
                reply = str(payload.get("reply") or "").strip()
                if not isinstance(updated, dict) or "action" not in updated or "args" not in updated:
                    raise ValueError("The pending update response did not include a valid pending action.")
                if not reply:
                    reply = f"Updated the pending {_summarize_action(updated.get('action'))}. Reply `YES` to approve it or `NO` to cancel it."
                set_pending_action(state["user_id"], updated)
                self._remember_turn(state["user_id"], user_text, reply)
                return {"final_response": reply}
            except Exception:
                reply = (
                    "I understand you want to change the pending action, but I could not safely update the queued details. "
                    "Please say the full requested change, or reply `NO` to cancel the pending action and start again."
                )
                return {"final_response": reply}
        def approval_node(state: AgentState) -> AgentState:
            pending = state.get("pending_action")
            decision = approval_decision(state.get("user_text", ""))

            if not pending:
                return {"final_response": "I do not have a stored pending action to approve. I will not treat `YES` as approval unless an action has actually been queued. Please send the task again so I can queue it properly."}
            if decision == "NO":
                clear_pending_action(state["user_id"])
                return {"final_response": "Cancelled the pending action."}
            if decision != "YES":
                return {"final_response": "You have a pending action. Reply `YES` to approve it or `NO` to cancel it before starting another task."}

            try:
                result = execute_action(self.settings, state["user_id"], pending["action"], pending.get("args", {}))
                clear_pending_action(state["user_id"])
                response = _format_completed_action(pending["action"], result)
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
                update_user_memory(
                    state["user_id"],
                    last_failed_task={
                        "user_text": state.get("user_text", ""),
                        "pending_action": pending,
                        "error": str(error),
                    },
                )
                return {
                    "final_response": (
                        f"I tried to run the approved `{pending.get('action')}` action, but it failed:\n"
                        f"`{error}`\n\nI kept the action pending so it can be retried after repair."
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
                f"Pending approval action, if any: {state.get('pending_action')}\n"
                f"Current Discord attachments: {attachments_summary}\n"
                f"Allowed workspace root: {self.settings.workspace_root}\n"
                "The user's latest message follows. If the user asks about memory, status, pending actions, or a failed/previous task, answer conversationally from memory and pending action context without calling tools. If the user asks to summarize/analyze/explain a file here in chat, call analyze_file_tool and then answer directly in Discord. Only queue generate_technical_report_tool when the user explicitly asks to save/create/export/write a report file."
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

        def route_after_tools(state: AgentState) -> Literal["agent", "final"]:
            for message in reversed(state.get("messages", [])):
                if isinstance(message, ToolMessage):
                    try:
                        payload = json.loads(str(message.content))
                    except json.JSONDecodeError:
                        payload = {}
                    if payload.get("status") in {"approval_required", "plan_ready"}:
                        return "final"
                    continue
                break
            return "agent"

        def final_node(state: AgentState) -> AgentState:
            for message in reversed(state.get("messages", [])):
                if isinstance(message, ToolMessage):
                    content = str(message.content)
                    try:
                        payload = json.loads(content)
                    except json.JSONDecodeError:
                        payload = {}
                    if payload.get("status") in {"approval_required", "plan_ready"}:
                        formatted = format_tool_response_for_discord(content)
                        self._remember_turn(state["user_id"], state["user_text"], formatted)
                        return {"final_response": formatted}
                    continue
                break

            for message in reversed(state.get("messages", [])):
                if isinstance(message, AIMessage):
                    content = format_tool_response_for_discord(str(message.content))
                    self._remember_turn(state["user_id"], state["user_text"], content)
                    return {"final_response": content}
            return {"final_response": "Done."}

        graph = StateGraph(AgentState)
        graph.add_node("load_context", load_context)
        graph.add_node("classify", classify_node)
        graph.add_node("approval", approval_node)
        graph.add_node("status", status_node)
        graph.add_node("pending_update", pending_update_node)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", ToolNode(tools))
        graph.add_node("final", final_node)
        graph.add_edge(START, "load_context")
        graph.add_edge("load_context", "classify")
        graph.add_conditional_edges("classify", route_after_context)
        graph.add_edge("status", END)
        graph.add_edge("pending_update", END)
        graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: "final"})
        graph.add_conditional_edges("tools", route_after_tools, {"agent": "agent", "final": "final"})
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
