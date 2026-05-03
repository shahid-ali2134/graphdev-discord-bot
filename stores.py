import json
from pathlib import Path
from typing import Any


APP_DATA_DIR = Path(".graphdev")
MEMORY_FILE = APP_DATA_DIR / "memory.json"
PENDING_FILE = APP_DATA_DIR / "pending_actions.json"


def _read_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_user_memory(user_id: str) -> dict[str, Any]:
    data = _read_json(MEMORY_FILE, {"users": {}})
    users = data.setdefault("users", {})
    if user_id not in users:
        users[user_id] = {
            "active_project": None,
            "active_folder": None,
            "active_file": None,
            "recent_paths": [],
            "conversation_summary": "",
        }
        _write_json(MEMORY_FILE, data)
    return users[user_id]


def update_user_memory(user_id: str, **updates: Any) -> dict[str, Any]:
    data = _read_json(MEMORY_FILE, {"users": {}})
    users = data.setdefault("users", {})
    memory = users.setdefault(user_id, get_user_memory(user_id))
    memory.update(updates)
    _write_json(MEMORY_FILE, data)
    return memory


def remember_path(user_id: str, path: str) -> None:
    memory = get_user_memory(user_id)
    recent = [path, *[item for item in memory.get("recent_paths", []) if item != path]]
    update_user_memory(user_id, recent_paths=recent[:10])


def get_pending_action(user_id: str) -> dict[str, Any] | None:
    data = _read_json(PENDING_FILE, {"users": {}})
    return data.get("users", {}).get(user_id)


def set_pending_action(user_id: str, action: dict[str, Any]) -> dict[str, Any]:
    data = _read_json(PENDING_FILE, {"users": {}})
    data.setdefault("users", {})[user_id] = action
    _write_json(PENDING_FILE, data)
    return action


def clear_pending_action(user_id: str) -> None:
    data = _read_json(PENDING_FILE, {"users": {}})
    data.setdefault("users", {}).pop(user_id, None)
    _write_json(PENDING_FILE, data)
