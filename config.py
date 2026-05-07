import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    discord_token: str
    anthropic_api_key: str
    claude_model: str = "claude-sonnet-4-6"
    workspace_root: Path = Path(r"F:\Upwork")
    app_data_dir: Path = Path(".graphdev")
    max_discord_message_length: int = 1900


def load_settings(require_secrets: bool = True) -> Settings:
    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    claude_model = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6").strip()
    workspace_root = Path(os.getenv("GRAPHDEV_WORKSPACE_ROOT", r"F:\Upwork")).resolve()

    if require_secrets:
        missing = []
        if not discord_token:
            missing.append("DISCORD_TOKEN")
        if not anthropic_api_key:
            missing.append("ANTHROPIC_API_KEY")
        if missing:
            names = ", ".join(missing)
            raise RuntimeError(f"Missing required environment variable(s): {names}")

    workspace_root.mkdir(parents=True, exist_ok=True)

    return Settings(
        discord_token=discord_token,
        anthropic_api_key=anthropic_api_key,
        claude_model=claude_model,
        workspace_root=workspace_root,
    )
