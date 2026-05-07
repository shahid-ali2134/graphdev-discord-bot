from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

import document_tools
import workspace
from config import Settings
from stores import remember_path, update_user_memory


# ---------------------------------------------------------------------------
# ML pipeline helpers
# ---------------------------------------------------------------------------

def _get_dataset_preview(path: Path) -> str:
    if not path.exists():
        return "File not found at the given path."
    ext = path.suffix.lower()
    if ext in {".csv", ".tsv"}:
        try:
            import csv as _csv
            sep = "\t" if ext == ".tsv" else ","
            with open(path, encoding="utf-8", errors="replace") as f:
                reader = _csv.reader(f, delimiter=sep)
                rows = [next(reader, []) for _ in range(5)]
            headers = rows[0] if rows else []
            samples = rows[1:4]
            return f"Columns ({len(headers)}): {headers}\nSample rows: {samples}"
        except Exception as exc:
            return f"CSV read error: {exc}"
    return f"File type: {ext}, size: {path.stat().st_size} bytes"


def _build_notebook_prompt(dataset_path: str, task: str, preview: str) -> str:
    return f"""Generate a complete, self-contained Jupyter notebook (.ipynb JSON) for this ML analysis.

Dataset path: {dataset_path}
Task: {task}
Dataset preview:
{preview}

Include these cells IN ORDER:
1. **Imports** — pandas, numpy, matplotlib, seaborn, sklearn; try xgboost, fall back to GradientBoosting
2. **Data Loading** — load from '{dataset_path}', print shape, .head(5), .info(), .describe()
3. **EDA** — distribution plots, correlation heatmap, missing-value bar chart (save all figures with plt.savefig)
4. **Preprocessing** — impute missing values, encode categoricals (label or one-hot), scale features (StandardScaler)
5. **Train/Test Split** — sklearn train_test_split, stratify if classification
6. **Model 1 — Baseline** — LogisticRegression or LinearRegression depending on task
7. **Model 2 — Random Forest** — RandomForestClassifier or RandomForestRegressor
8. **Model 3 — Gradient Boosting** — try xgboost first, fall back to sklearn GradientBoosting
9. **Evaluation** — metrics per model; confusion matrix if classification; feature importance bar chart
10. **Results Summary** — print a comparison table of all models with their key metrics

Rules:
- Use plt.savefig('<name>.png') for every chart; include plt.close() after each
- Print metric lines like "Random Forest — Accuracy: 0.91" so they appear in notebook output
- Wrap xgboost import in try/except; fall back to sklearn.ensemble.GradientBoostingClassifier/Regressor
- The notebook must be runnable with python kernel; no user interaction required

Return ONLY valid .ipynb JSON. No markdown fences, no explanation — raw JSON starting with {{."""


def execute_ml_pipeline(settings: Settings, user_id: str, args: dict[str, Any]) -> str:
    from langchain_anthropic import ChatAnthropic
    from langchain_core.messages import HumanMessage

    dataset_path_str = args.get("dataset_path", "")
    task_description = args.get("task_description", "ML analysis")
    notebook_path_str = args.get("notebook_path", "")
    report_path_str = args.get("report_path", "")
    base = args.get("base")

    dataset_path = workspace.resolve_workspace_path(settings, dataset_path_str, base)
    notebook_path = workspace.resolve_workspace_path(settings, notebook_path_str, base)

    if report_path_str:
        report_path = workspace.resolve_workspace_path(settings, report_path_str, base)
    else:
        report_path = workspace.ensure_inside_root(
            settings, notebook_path.parent / (notebook_path.stem + "_report.docx")
        )

    preview = _get_dataset_preview(dataset_path)

    llm = ChatAnthropic(
        model=settings.claude_model,
        api_key=settings.anthropic_api_key,
        temperature=0,
        max_tokens=8192,
    )
    prompt = _build_notebook_prompt(dataset_path_str, task_description, preview)
    response = llm.invoke([HumanMessage(content=prompt)])
    raw = str(response.content).strip()

    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0].strip()

    try:
        notebook_data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise workspace.WorkspaceError(f"Generated notebook is not valid JSON: {exc}") from exc

    notebook_path.parent.mkdir(parents=True, exist_ok=True)
    notebook_path.write_text(json.dumps(notebook_data, indent=1), encoding="utf-8")
    remember_path(user_id, workspace.relative(settings, notebook_path))

    run_result = workspace.run_notebook(settings, notebook_path, timeout=600)
    run_ok = run_result.get("returncode") == 0

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_instructions = (
        "Create a professional technical report with these sections: "
        "Executive Summary, Dataset Overview, Preprocessing Techniques, "
        "ML Models and Hyperparameters, Evaluation Metrics and Results, "
        "Model Comparison Table, Visualizations Summary, Conclusion. "
        f"The notebook execution {'succeeded' if run_ok else 'encountered errors'}."
    )
    report_content = document_tools.build_technical_report(settings, notebook_path, report_instructions)
    report_result = document_tools.write_docx_content(
        settings, report_path, report_content,
        overwrite=True,
        style_hint="Professional ML technical report, Times New Roman",
    )
    remember_path(user_id, workspace.relative(settings, report_path))

    result: dict[str, Any] = {
        "notebook": workspace.relative(settings, notebook_path),
        "notebook_executed": run_ok,
        "report": report_result.get("path"),
    }
    if not run_ok:
        result["execution_errors"] = run_result.get("stderr", "")[-2000:]
    return workspace.as_json(result)


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def build_tools(
    settings: Settings,
    user_id: str,
    attachments: list[dict[str, Any]] | None = None,
    original_request: str = "",
):
    attachments = attachments or []

    # -- Read-only / inspection tools ----------------------------------------

    @tool
    def resolve_project_tool(query: str) -> str:
        """Resolve a natural-language project or folder name under F:\\Upwork. Sets the active project when there is one strong match."""
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
        """List the folder tree for a project or folder under F:\\Upwork."""
        target = workspace.resolve_workspace_path(settings, path or ".")
        rows = workspace.scan_tree(settings, target, max_entries)
        remember_path(user_id, workspace.relative(settings, target))
        return "\n".join(rows) if rows else "[empty folder]"

    @tool
    def read_file_tool(path: str, base: str = "") -> str:
        """Read a text, code, or document file under F:\\Upwork. Secret files are redacted."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        remember_path(user_id, workspace.relative(settings, target))
        return workspace.read_text_file(settings, target)

    @tool
    def analyze_file_tool(path: str, base: str = "", instructions: str = "") -> str:
        """Return a concise technical summary in chat for a source file, notebook, PDF, DOCX, PPTX, spreadsheet, or text file. Does not write any files."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        remember_path(user_id, workspace.relative(settings, target))
        return document_tools.build_chat_summary(settings, target, instructions or original_request)

    @tool
    def profile_attachment_tool(path: str) -> str:
        """Profile a saved attachment or any workspace file — size, type, columns, preview — without exposing secrets."""
        target = workspace.resolve_workspace_path(settings, path)
        return workspace.as_json(workspace.profile_path(settings, target))

    @tool
    def generate_project_plan_tool(request: str, project_path: str = "") -> str:
        """Produce a concise implementation plan after scanning and reading relevant files. Does not write any files."""
        return workspace.as_json({
            "request": request,
            "project_path": project_path,
            "instruction": "Scan and read relevant files, then propose concrete changes. Execute writes with write_file_tool or modify_file_tool once the plan is clear.",
        })

    # -- File write / modify tools -------------------------------------------

    @tool
    def write_file_tool(path: str, content: str, base: str = "", overwrite: bool = False) -> str:
        """Write a new file under F:\\Upwork. Pass overwrite=true to replace an existing file (a backup is created). For .docx files use write_docx_tool instead."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        if workspace.is_secret_path(target):
            raise workspace.WorkspaceError("Refusing to write secret or .git files.")
        result = workspace.write_text_file(settings, target, content, overwrite)
        remember_path(user_id, result["path"])
        return workspace.as_json(result)

    @tool
    def modify_file_tool(path: str, new_content: str, base: str = "", summary: str = "") -> str:
        """Replace an existing text file with new content. A backup and diff are created automatically. For .docx files use modify_docx_tool instead."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        if workspace.is_secret_path(target):
            raise workspace.WorkspaceError("Refusing to modify secret or .git files.")
        result = workspace.modify_text_file(settings, target, new_content)
        remember_path(user_id, result["path"])
        return workspace.as_json(result)

    @tool
    def batch_modify_tool(changes_json: str, summary: str = "", base: str = "") -> str:
        """Apply coordinated edits to multiple existing files in one call. changes_json is a JSON array of {path, new_content} objects. Use this when a feature addition or refactor touches more than one file."""
        changes = json.loads(changes_json)
        if not isinstance(changes, list) or not changes:
            raise workspace.WorkspaceError("changes_json must be a non-empty JSON array of {path, new_content} objects.")
        results = []
        for change in changes:
            p = workspace.resolve_workspace_path(settings, change["path"], base or None)
            if workspace.is_secret_path(p):
                raise workspace.WorkspaceError(f"Refusing to modify secret or .git file: {change['path']}")
            if p.exists():
                result = workspace.modify_text_file(settings, p, change["new_content"])
            else:
                result = workspace.write_text_file(settings, p, change["new_content"], overwrite=False)
            remember_path(user_id, result["path"])
            results.append(result["path"])
        return workspace.as_json({"modified": results, "count": len(results)})

    @tool
    def batch_create_files_tool(files_json: str, base: str = "", overwrite: bool = False) -> str:
        """Create multiple new files in one call. files_json is a JSON object mapping relative paths to content strings. Use when a feature requires several new files."""
        files = json.loads(files_json)
        if not isinstance(files, dict) or not files:
            raise workspace.WorkspaceError("files_json must be a non-empty JSON object mapping paths to content strings.")
        results = []
        for rel_path, content in files.items():
            p = workspace.resolve_workspace_path(settings, rel_path, base or None)
            if workspace.is_secret_path(p):
                raise workspace.WorkspaceError(f"Refusing to create secret or .git file: {rel_path}")
            result = workspace.write_text_file(settings, p, content, overwrite=overwrite)
            remember_path(user_id, result["path"])
            results.append(result["path"])
        return workspace.as_json({"created": results, "count": len(results)})

    @tool
    def delete_path_tool(path: str, base: str = "") -> str:
        """Delete a file or folder under F:\\Upwork. A backup is created before deletion."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        result = workspace.delete_path(settings, target)
        return workspace.as_json(result)

    @tool
    def create_project_tool(name: str, files_json: str = "{}") -> str:
        """Create a new project folder with optional initial files. files_json maps relative paths to text content."""
        files = json.loads(files_json or "{}")
        result = workspace.create_project(settings, name, files)
        update_user_memory(user_id, active_project=result["project"], active_folder=result["project"])
        remember_path(user_id, result["project"])
        return workspace.as_json(result)

    @tool
    def create_backup_tool(path: str, base: str = "") -> str:
        """Create an immediate backup of a file or folder under F:\\Upwork."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        backup = workspace.make_backup(settings, target)
        return workspace.as_json({"backup": workspace.relative(settings, backup)})

    @tool
    def rollback_tool(backup_path: str, destination: str = "") -> str:
        """Restore a previous .graphdev_backups path back into the workspace."""
        backup = workspace.resolve_workspace_path(settings, backup_path)
        dest = workspace.resolve_workspace_path(settings, destination) if destination else None
        result = workspace.restore_backup(settings, backup, dest)
        return workspace.as_json(result)

    # -- Document tools -------------------------------------------------------

    @tool
    def write_docx_tool(path: str, content: str, base: str = "", overwrite: bool = False, style_hint: str = "") -> str:
        """Create a real .docx Word file from Markdown-like content. Default style: Times New Roman, title 28pt centered, body 11pt, Heading 1 16pt bold, Heading 2 14pt bold."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        if target.suffix.lower() != ".docx":
            raise workspace.WorkspaceError("write_docx_tool only supports .docx files.")
        result = document_tools.write_docx_content(settings, target, content, overwrite=overwrite, style_hint=style_hint)
        remember_path(user_id, result["path"])
        return workspace.as_json(result)

    @tool
    def modify_docx_tool(path: str, mode: str, content: str, marker: str = "", style_hint: str = "", base: str = "") -> str:
        """Edit an existing .docx Word file while preserving existing styles. mode=append_markdown for new sections/bullets, mode=replace_paragraph to replace a paragraph containing marker text."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        if target.suffix.lower() != ".docx":
            raise workspace.WorkspaceError("modify_docx_tool only supports .docx files.")
        result = document_tools.modify_docx(settings, target, mode, content, marker=marker, style_hint=style_hint)
        remember_path(user_id, result["path"])
        return workspace.as_json(result)

    @tool
    def generate_technical_report_tool(path: str, report_path: str = "", instructions: str = "", base: str = "", overwrite: bool = False) -> str:
        """Generate a detailed technical report for a code file, notebook, PDF, DOCX, or spreadsheet and save it to disk. Use .docx in report_path for a real Word file."""
        source = workspace.resolve_workspace_path(settings, path, base or None)
        report_content = document_tools.build_technical_report(settings, source, instructions or original_request)
        if not report_path:
            report_path = str(source.with_suffix(".technical_report.md"))
        target = workspace.resolve_workspace_path(settings, report_path, base or None)
        if target.suffix.lower() == ".docx":
            result = document_tools.write_docx_content(
                settings, target, report_content,
                overwrite=overwrite or target.exists(),
                style_hint=instructions,
            )
        else:
            result = workspace.write_text_file(settings, target, report_content, overwrite=overwrite or target.exists())
        remember_path(user_id, result["path"])
        return workspace.as_json(result)

    # -- Execution tools ------------------------------------------------------

    @tool
    def run_file_tool(path: str, base: str = "", script_args: list[str] | None = None, timeout: int = 120) -> str:
        """Execute a Python script under F:\\Upwork and return its output. Pass command-line arguments via script_args."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        result = workspace.run_python_file(settings, target, script_args, timeout)
        return workspace.as_json(result)

    @tool
    def run_notebook_tool(path: str, base: str = "", timeout: int = 300) -> str:
        """Execute a Jupyter notebook and save real cell outputs back into the notebook file."""
        target = workspace.resolve_workspace_path(settings, path, base or None)
        result = workspace.run_notebook(settings, target, timeout)
        return workspace.as_json(result)

    @tool
    def install_dependencies_tool(project: str, packages: list[str]) -> str:
        """Install packages into the selected project's own .venv using pip."""
        project_path = workspace.resolve_workspace_path(settings, project)
        result = workspace.install_dependencies(settings, project_path, packages)
        return workspace.as_json(result)

    # -- Attachment tool ------------------------------------------------------

    @tool
    def save_attachment_tool(destination: str = "") -> str:
        """Save the Discord attachments from this message (or recent uploads) to a workspace folder."""
        dest = workspace.resolve_workspace_path(settings, destination or ".")
        dest.mkdir(parents=True, exist_ok=True)
        saved = []

        sources = list(attachments)
        if not sources:
            # Fall back to recent uploads stored in memory
            from stores import get_user_memory
            memory = get_user_memory(user_id)
            for p in memory.get("recent_uploads", []):
                candidate = Path(p)
                if candidate.exists():
                    sources.append({"filename": candidate.name, "local_path": str(candidate)})

        for attachment in sources:
            filename = Path(str(attachment.get("filename", "attachment"))).name
            target = workspace.ensure_inside_root(settings, dest / filename)

            local_path = attachment.get("local_path")
            url = attachment.get("url")

            if local_path and Path(local_path).exists():
                src = workspace.ensure_inside_root(settings, Path(local_path))
                if target.exists():
                    workspace.make_backup(settings, target)
                shutil.copy2(src, target)
            elif url:
                import urllib.request
                with urllib.request.urlopen(url, timeout=60) as response:
                    if target.exists():
                        workspace.make_backup(settings, target)
                    target.write_bytes(response.read())
            else:
                raise workspace.WorkspaceError(f"Attachment has no local_path or url: {filename}")

            rel = workspace.relative(settings, target)
            saved.append(rel)
            remember_path(user_id, rel)

        # Clear recent_uploads after successful save
        update_user_memory(user_id, recent_uploads=[])
        return workspace.as_json({"saved": saved})

    # -- ML pipeline tool -----------------------------------------------------

    @tool
    def run_ml_pipeline_tool(
        dataset_path: str,
        task_description: str,
        notebook_path: str,
        report_path: str = "",
        base: str = "",
    ) -> str:
        """Run a complete end-to-end ML pipeline: generates a Jupyter notebook (EDA, preprocessing, 3+ models, evaluation), executes all cells, and creates a Word report. Use for 'take this dataset, build a notebook, run it, give me a Word report' requests."""
        workspace.resolve_workspace_path(settings, dataset_path, base or None)
        workspace.resolve_workspace_path(settings, notebook_path, base or None)
        if not report_path:
            nb = workspace.resolve_workspace_path(settings, notebook_path, base or None)
            report_path = str(nb.parent / (nb.stem + "_report.docx"))
        return execute_ml_pipeline(settings, user_id, {
            "dataset_path": dataset_path,
            "task_description": task_description,
            "notebook_path": notebook_path,
            "report_path": report_path,
            "base": base or None,
        })

    return [
        resolve_project_tool,
        scan_project_tool,
        read_file_tool,
        analyze_file_tool,
        profile_attachment_tool,
        generate_project_plan_tool,
        write_file_tool,
        modify_file_tool,
        batch_modify_tool,
        batch_create_files_tool,
        delete_path_tool,
        create_project_tool,
        create_backup_tool,
        rollback_tool,
        write_docx_tool,
        modify_docx_tool,
        generate_technical_report_tool,
        run_file_tool,
        run_notebook_tool,
        install_dependencies_tool,
        save_attachment_tool,
        run_ml_pipeline_tool,
    ]
