from __future__ import annotations

import json
import shutil
import urllib.request
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

import workspace
from config import Settings
from stores import remember_path, set_pending_action, update_user_memory


RISKY_ACTIONS = {
    "create_project",
    "write_file",
    "modify_file",
    "delete_path",
    "install_dependencies",
    "run_file",
    "run_notebook",
    "rollback",
    "save_attachment",
}


def _safe_approval_args(args: dict[str, Any]) -> dict[str, Any]:
    safe = dict(args)
    for key in ("content", "new_content", "files", "attachments"):
        if key in safe:
            value = safe[key]
            if isinstance(value, str):
                safe[key] = f"[{len(value)} chars queued]"
            elif isinstance(value, dict):
                safe[key] = f"[{len(value)} file(s) queued]"
            elif isinstance(value, list):
                safe[key] = f"[{len(value)} item(s) queued]"
            else:
                safe[key] = "[queued]"
    return safe


def approval_message(action: str, args: dict[str, Any], reason: str) -> str:
    return workspace.as_json(
        {
            "status": "approval_required",
            "action": action,
            "reason": reason,
            "reply": "Reply YES to approve or NO to cancel.",
            "arguments": _safe_approval_args(args),
        }
    )


def queue_action(
    user_id: str,
    action: str,
    args: dict[str, Any],
    reason: str,
    original_request: str = "",
    original_attachments: list[dict[str, Any]] | None = None,
) -> str:
    set_pending_action(
        user_id,
        {
            "action": action,
            "args": args,
            "reason": reason,
            "original_request": original_request,
            "original_attachments": original_attachments or [],
            "continue_after_approval": True,
        },
    )
    return approval_message(action, args, reason)


def execute_action(settings: Settings, user_id: str, action: str, args: dict[str, Any]) -> str:
    if action == "create_project":
        result = workspace.create_project(settings, args["name"], args.get("files"))
        update_user_memory(user_id, active_project=result["project"], active_folder=result["project"])
        remember_path(user_id, result["project"])
        return workspace.as_json(result)
    if action == "write_file":
        base = args.get("base")
        path = workspace.resolve_workspace_path(settings, args["path"], base=base)
        result = workspace.write_text_file(settings, path, args["content"], bool(args.get("overwrite", False)))
        remember_path(user_id, result["path"])
        return workspace.as_json(result)
    if action == "modify_file":
        path = workspace.resolve_workspace_path(settings, args["path"], base=args.get("base"))
        result = workspace.modify_text_file(settings, path, args["new_content"])
        remember_path(user_id, result["path"])
        return workspace.as_json(result)
    if action == "delete_path":
        path = workspace.resolve_workspace_path(settings, args["path"], base=args.get("base"))
        return workspace.as_json(workspace.delete_path(settings, path))
    if action == "install_dependencies":
        project = workspace.resolve_workspace_path(settings, args.get("project"))
        return workspace.as_json(workspace.install_dependencies(settings, project, args["packages"]))
    if action == "run_file":
        path = workspace.resolve_workspace_path(settings, args["path"], base=args.get("base"))
        return workspace.as_json(workspace.run_python_file(settings, path, args.get("args"), int(args.get("timeout", 120))))
    if action == "run_notebook":
        path = workspace.resolve_workspace_path(settings, args["path"], base=args.get("base"))
        return workspace.as_json(workspace.run_notebook(settings, path, int(args.get("timeout", 300))))
    if action == "rollback":
        backup = workspace.resolve_workspace_path(settings, args["backup_path"])
        destination = workspace.resolve_workspace_path(settings, args["destination"]) if args.get("destination") else None
        return workspace.as_json(workspace.restore_backup(settings, backup, destination))
    if action == "save_attachment":
        return workspace.as_json(save_attachment_impl(settings, user_id, args))
    raise workspace.WorkspaceError(f"Unknown pending action: {action}")


def save_attachment_impl(settings: Settings, user_id: str, args: dict[str, Any]) -> dict[str, Any]:
    attachments = args.get("attachments") or []
    destination = workspace.resolve_workspace_path(settings, args.get("destination") or ".")
    destination.mkdir(parents=True, exist_ok=True)
    saved = []

    for attachment in attachments:
        filename = Path(str(attachment.get("filename", "attachment"))).name
        target = workspace.ensure_inside_root(settings, destination / filename)
        if target.exists():
            raise workspace.WorkspaceError(f"Refusing to overwrite uploaded attachment: {workspace.relative(settings, target)}")

        local_path = attachment.get("local_path")
        url = attachment.get("url")
        if local_path:
            source = workspace.ensure_inside_root(settings, Path(local_path))
            shutil.copy2(source, target)
        elif url:
            with urllib.request.urlopen(url, timeout=60) as response:
                target.write_bytes(response.read())
        else:
            raise workspace.WorkspaceError(f"Attachment has no local_path or url: {filename}")

        rel = workspace.relative(settings, target)
        saved.append(rel)
        remember_path(user_id, rel)

    return {"saved": saved}


def build_tools(
    settings: Settings,
    user_id: str,
    attachments: list[dict[str, Any]] | None = None,
    original_request: str = "",
):
    attachments = attachments or []

    @tool
    def resolve_project_tool(query: str) -> str:
        """Resolve a natural-language project/folder name under F:\\Upwork and set it active when there is one strong match."""
        root = settings.workspace_root
        query_lower = query.lower().strip()
        candidates = []
        for item in root.iterdir():
            if item.is_dir() and query_lower in item.name.lower():
                candidates.append(item)
        if not candidates and query_lower in {"this project", "current project", "active project", "previous folder"}:
            from stores import get_user_memory

            memory = get_user_memory(user_id)
            active = memory.get("active_project") or memory.get("active_folder")
            if active:
                candidates.append(workspace.resolve_workspace_path(settings, active))
        result = [{"path": workspace.relative(settings, item), "name": item.name} for item in candidates[:10]]
        if len(result) == 1:
            update_user_memory(user_id, active_project=result[0]["path"], active_folder=result[0]["path"])
            remember_path(user_id, result[0]["path"])
        return workspace.as_json({"matches": result, "active_set": len(result) == 1})

    @tool
    def scan_project_tool(path: str = "", max_entries: int = 200) -> str:
        """List the folder tree for a project/folder under F:\\Upwork."""
        target = workspace.resolve_workspace_path(settings, path or ".")
        rows = workspace.scan_tree(settings, target, max_entries)
        remember_path(user_id, workspace.relative(settings, target))
        return "\n".join(rows) if rows else "[empty folder]"

    @tool
    def read_file_tool(path: str, base: str = "") -> str:
        """Read a text/code/document file under F:\\Upwork. Secret files are redacted."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        remember_path(user_id, workspace.relative(settings, target))
        return workspace.read_text_file(settings, target)

    @tool
    def create_project_tool(name: str, files_json: str = "{}") -> str:
        """Queue creation of a project folder and optional initial files. files_json maps relative paths to text content."""
        files = json.loads(files_json or "{}")
        workspace.resolve_workspace_path(settings, name)
        return queue_action(user_id, "create_project", {"name": name, "files": files}, "Creating files requires approval.", original_request, attachments)

    @tool
    def write_file_tool(path: str, content: str, base: str = "", overwrite: bool = False) -> str:
        """Queue writing a new file under F:\\Upwork. Existing files require overwrite=true or modify_file_tool."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        if workspace.is_secret_path(target):
            raise workspace.WorkspaceError("Refusing to write secret or .git files.")
        args = {"path": path, "content": content, "base": base or None, "overwrite": overwrite}
        return queue_action(user_id, "write_file", args, "Writing files requires approval.", original_request, attachments)

    @tool
    def modify_file_tool(path: str, new_content: str, base: str = "", summary: str = "") -> str:
        """Queue replacement of an existing text file with new content. A backup and diff will be created on approval."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        if workspace.is_secret_path(target):
            raise workspace.WorkspaceError("Refusing to modify secret or .git files.")
        args = {"path": path, "new_content": new_content, "base": base or None, "summary": summary}
        return queue_action(user_id, "modify_file", args, "Modifying existing files requires approval and creates a backup.", original_request, attachments)

    @tool
    def delete_path_tool(path: str, base: str = "") -> str:
        """Queue deletion of a file or folder under F:\\Upwork. A backup is created before deletion."""
        workspace.resolve_workspace_path(settings, path, base or None)
        return queue_action(user_id, "delete_path", {"path": path, "base": base or None}, "Deleting files or folders requires explicit approval.", original_request, attachments)

    @tool
    def create_backup_tool(path: str, base: str = "") -> str:
        """Create an immediate backup of a file or folder under F:\\Upwork."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        backup = workspace.make_backup(settings, target)
        return workspace.as_json({"backup": workspace.relative(settings, backup)})

    @tool
    def rollback_tool(backup_path: str, destination: str = "") -> str:
        """Queue restoring a previous .graphdev_backups path back into the workspace."""
        workspace.resolve_workspace_path(settings, backup_path)
        if destination:
            workspace.resolve_workspace_path(settings, destination)
        return queue_action(user_id, "rollback", {"backup_path": backup_path, "destination": destination or None}, "Rollback changes files and requires approval.", original_request, attachments)

    @tool
    def install_dependencies_tool(project: str, packages: list[str]) -> str:
        """Queue installing packages into the selected project's own .venv."""
        workspace.resolve_workspace_path(settings, project)
        return queue_action(user_id, "install_dependencies", {"project": project, "packages": packages}, "Installing dependencies executes package manager code and requires approval.", original_request, attachments)

    @tool
    def run_file_tool(path: str, base: str = "", args: list[str] | None = None, timeout: int = 120) -> str:
        """Queue execution of a Python script under F:\\Upwork."""
        workspace.resolve_workspace_path(settings, path, base or None)
        return queue_action(user_id, "run_file", {"path": path, "base": base or None, "args": args or [], "timeout": timeout}, "Executing code requires approval.", original_request, attachments)

    @tool
    def run_notebook_tool(path: str, base: str = "", timeout: int = 300) -> str:
        """Queue execution of a Jupyter notebook and save real cell outputs back to the notebook."""
        workspace.resolve_workspace_path(settings, path, base or None)
        return queue_action(user_id, "run_notebook", {"path": path, "base": base or None, "timeout": timeout}, "Executing notebooks requires approval.", original_request, attachments)

    @tool
    def save_attachment_tool(destination: str = "") -> str:
        """Queue saving current Discord attachments into a workspace folder without overwriting existing files."""
        workspace.resolve_workspace_path(settings, destination or ".")
        return queue_action(
            user_id,
            "save_attachment",
            {"destination": destination or ".", "attachments": attachments},
            "Saving Discord attachments writes files and requires approval.",
            original_request,
            attachments,
        )

    @tool
    def profile_attachment_tool(path: str) -> str:
        """Profile a saved attachment or any workspace file without exposing secrets."""
        target = workspace.resolve_workspace_path(settings, path)
        return workspace.as_json(workspace.profile_path(settings, target))

    @tool
    def generate_project_plan_tool(request: str, project_path: str = "") -> str:
        """Create a concise implementation plan after scanning/reading relevant files. This tool does not write files."""
        return workspace.as_json(
            {
                "request": request,
                "project_path": project_path,
                "instruction": "Use scan_project_tool and read_file_tool to inspect relevant files, then propose concrete changes. Queue writes with write_file_tool or modify_file_tool only after the plan is clear.",
            }
        )

    @tool
    def apply_project_plan_tool(plan_json: str) -> str:
        """Summarize that a plan is ready to apply. Use write/modify/delete/install/run tools for the actual queued actions."""
        return workspace.as_json({"status": "plan_ready", "plan": json.loads(plan_json)})

    return [
        resolve_project_tool,
        scan_project_tool,
        read_file_tool,
        create_project_tool,
        write_file_tool,
        modify_file_tool,
        delete_path_tool,
        create_backup_tool,
        rollback_tool,
        install_dependencies_tool,
        run_file_tool,
        run_notebook_tool,
        save_attachment_tool,
        profile_attachment_tool,
        generate_project_plan_tool,
        apply_project_plan_tool,
    ]
