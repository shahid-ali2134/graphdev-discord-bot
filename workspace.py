from __future__ import annotations

import difflib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from config import Settings


SECRET_FILENAMES = {".env", ".env.local", ".env.production", ".env.development"}
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss", ".json",
    ".md", ".txt", ".yaml", ".yml", ".toml", ".ini", ".csv", ".sql", ".sh",
    ".ps1", ".bat", ".java", ".cs", ".cpp", ".c", ".h", ".rs", ".go", ".php",
    ".rb", ".swift", ".kt", ".xml", ".ipynb",
}


class WorkspaceError(RuntimeError):
    pass


def ensure_inside_root(settings: Settings, path: Path) -> Path:
    root = settings.workspace_root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"Path is outside the allowed workspace root: {root}") from exc
    return resolved


def resolve_workspace_path(settings: Settings, value: str | None, base: str | None = None) -> Path:
    root = settings.workspace_root.resolve()
    if not value:
        candidate = root
    else:
        raw = Path(value).expanduser()
        if raw.is_absolute():
            candidate = raw
        elif base:
            candidate = resolve_workspace_path(settings, base) / raw
        else:
            candidate = root / raw
    return ensure_inside_root(settings, candidate)


def relative(settings: Settings, path: Path) -> str:
    return str(ensure_inside_root(settings, path).relative_to(settings.workspace_root.resolve()))


def is_secret_path(path: Path) -> bool:
    return path.name.lower() in SECRET_FILENAMES or any(part.lower() == ".git" for part in path.parts)


def looks_textual(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name.lower() in {"dockerfile", "makefile"}


def scan_tree(settings: Settings, folder: Path, max_entries: int = 200) -> list[str]:
    folder = ensure_inside_root(settings, folder)
    if not folder.exists():
        raise WorkspaceError(f"Folder does not exist: {relative(settings, folder)}")
    if not folder.is_dir():
        raise WorkspaceError(f"Path is not a folder: {relative(settings, folder)}")

    ignored = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".next", "dist", "build"}
    rows = []
    for item in sorted(folder.rglob("*")):
        if len(rows) >= max_entries:
            rows.append("... truncated ...")
            break
        if any(part in ignored for part in item.relative_to(folder).parts):
            continue
        prefix = "[D]" if item.is_dir() else "[F]"
        rows.append(f"{prefix} {item.relative_to(folder)}")
    return rows


def read_text_file(settings: Settings, path: Path, max_chars: int = 12000) -> str:
    path = ensure_inside_root(settings, path)
    if is_secret_path(path):
        return "[redacted secret file]"
    if not path.exists():
        raise WorkspaceError(f"File does not exist: {relative(settings, path)}")
    if not path.is_file():
        raise WorkspaceError(f"Path is not a file: {relative(settings, path)}")
    if not looks_textual(path):
        raise WorkspaceError("This does not look like a text/code file. Profile the attachment or file instead.")
    content = path.read_text(encoding="utf-8", errors="replace")
    if len(content) > max_chars:
        return content[:max_chars] + "\n\n[truncated]"
    return content


def make_backup(settings: Settings, path: Path) -> Path:
    path = ensure_inside_root(settings, path)
    if not path.exists():
        raise WorkspaceError(f"Cannot back up missing path: {relative(settings, path)}")

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = settings.workspace_root / ".graphdev_backups" / timestamp
    backup_target = ensure_inside_root(settings, backup_root / path.relative_to(settings.workspace_root))
    backup_target.parent.mkdir(parents=True, exist_ok=True)

    if path.is_dir():
        shutil.copytree(path, backup_target)
    else:
        shutil.copy2(path, backup_target)
    return backup_target


def write_text_file(settings: Settings, path: Path, content: str, overwrite: bool = False) -> dict[str, Any]:
    path = ensure_inside_root(settings, path)
    if is_secret_path(path):
        raise WorkspaceError("Refusing to write secret or .git files.")
    if path.exists() and not overwrite:
        raise WorkspaceError("File exists. Use modify_file_tool for existing files or set overwrite explicitly.")

    backup = None
    if path.exists():
        backup = make_backup(settings, path)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return {
        "path": relative(settings, path),
        "backup": relative(settings, backup) if backup else None,
    }


def modify_text_file(settings: Settings, path: Path, new_content: str) -> dict[str, Any]:
    path = ensure_inside_root(settings, path)
    old_content = read_text_file(settings, path, max_chars=2_000_000)
    backup = make_backup(settings, path)
    path.write_text(new_content, encoding="utf-8")
    diff = "\n".join(
        difflib.unified_diff(
            old_content.splitlines(),
            new_content.splitlines(),
            fromfile=f"{relative(settings, path)} before",
            tofile=f"{relative(settings, path)} after",
            lineterm="",
        )
    )
    return {
        "path": relative(settings, path),
        "backup": relative(settings, backup),
        "diff": diff[:8000],
    }


def delete_path(settings: Settings, path: Path) -> dict[str, Any]:
    path = ensure_inside_root(settings, path)
    backup = make_backup(settings, path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return {"deleted": relative(settings, path), "backup": relative(settings, backup)}


def restore_backup(settings: Settings, backup_path: Path, destination: Path | None = None) -> dict[str, Any]:
    backup_path = ensure_inside_root(settings, backup_path)
    if ".graphdev_backups" not in backup_path.parts:
        raise WorkspaceError("Rollback source must be inside .graphdev_backups.")
    if not backup_path.exists():
        raise WorkspaceError("Backup path does not exist.")

    destination = destination or settings.workspace_root / Path(*backup_path.parts[backup_path.parts.index(".graphdev_backups") + 2:])
    destination = ensure_inside_root(settings, destination)
    if destination.exists():
        make_backup(settings, destination)
        if destination.is_dir():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if backup_path.is_dir():
        shutil.copytree(backup_path, destination)
    else:
        shutil.copy2(backup_path, destination)
    return {"restored": relative(settings, destination), "from": relative(settings, backup_path)}


def create_project(settings: Settings, name: str, files: dict[str, str] | None = None) -> dict[str, Any]:
    project_dir = resolve_workspace_path(settings, name)
    if project_dir.exists() and any(project_dir.iterdir()):
        raise WorkspaceError("Project folder already exists and is not empty.")
    project_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for file_path, content in (files or {}).items():
        target = resolve_workspace_path(settings, file_path, base=str(project_dir))
        if target.exists():
            raise WorkspaceError(f"Refusing to overwrite: {relative(settings, target)}")
        write_text_file(settings, target, content, overwrite=False)
        created.append(relative(settings, target))
    return {"project": relative(settings, project_dir), "created_files": created}


def run_python_file(settings: Settings, path: Path, args: list[str] | None = None, timeout: int = 120) -> dict[str, Any]:
    path = ensure_inside_root(settings, path)
    if path.suffix.lower() != ".py":
        raise WorkspaceError("run_file_tool currently executes Python scripts. Notebook execution uses run_notebook_tool.")
    cwd = path.parent
    project_venv_python = cwd / ".venv" / "Scripts" / "python.exe"
    python_exe = str(project_venv_python if project_venv_python.exists() else Path.cwd() / ".venv" / "Scripts" / "python.exe")
    if not Path(python_exe).exists():
        python_exe = "python"
    result = subprocess.run(
        [python_exe, str(path), *(args or [])],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
    return {
        "returncode": result.returncode,
        "stdout": result.stdout[-6000:],
        "stderr": result.stderr[-6000:],
    }


def run_notebook(settings: Settings, path: Path, timeout: int = 300) -> dict[str, Any]:
    path = ensure_inside_root(settings, path)
    if path.suffix.lower() != ".ipynb":
        raise WorkspaceError("Notebook execution requires an .ipynb file.")
    code = (
        "import nbformat, sys\n"
        "from nbclient import NotebookClient\n"
        "p = sys.argv[1]\n"
        "nb = nbformat.read(p, as_version=4)\n"
        "client = NotebookClient(nb, timeout=300, kernel_name='python3')\n"
        "client.execute()\n"
        "nbformat.write(nb, p)\n"
        "print('Notebook executed and outputs saved:', p)\n"
    )
    result = subprocess.run(
        ["python", "-c", code, str(path)],
        cwd=str(path.parent),
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
    return {"returncode": result.returncode, "stdout": result.stdout[-6000:], "stderr": result.stderr[-6000:]}


def install_dependencies(settings: Settings, project: Path, packages: list[str]) -> dict[str, Any]:
    project = ensure_inside_root(settings, project)
    project.mkdir(parents=True, exist_ok=True)
    venv_python = project / ".venv" / "Scripts" / "python.exe"

    if not venv_python.exists():
        subprocess.run(["python", "-m", "venv", str(project / ".venv")], cwd=str(project), check=True, shell=False)

    result = subprocess.run(
        [str(venv_python), "-m", "pip", "install", *packages],
        cwd=str(project),
        capture_output=True,
        text=True,
        timeout=600,
        shell=False,
    )
    return {"returncode": result.returncode, "stdout": result.stdout[-6000:], "stderr": result.stderr[-6000:]}


def profile_path(settings: Settings, path: Path) -> dict[str, Any]:
    path = ensure_inside_root(settings, path)
    if not path.exists():
        raise WorkspaceError("Path does not exist.")
    info = {
        "path": relative(settings, path),
        "is_dir": path.is_dir(),
        "size": path.stat().st_size if path.is_file() else None,
        "suffix": path.suffix.lower(),
    }
    if path.is_file() and looks_textual(path) and not is_secret_path(path):
        info["preview"] = read_text_file(settings, path, max_chars=2000)
    return info


def as_json(data: Any) -> str:
    return json.dumps(data, indent=2, default=str)
