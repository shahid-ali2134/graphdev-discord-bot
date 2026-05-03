import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    discord_token: str
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    command_prefix: str = "!graphdev"
    workspace_root: Path = Path(r"F:\Upwork")
    app_data_dir: Path = Path(".graphdev")
    max_discord_message_length: int = 1900


def load_settings(require_secrets: bool = True) -> Settings:
    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
    workspace_root = Path(os.getenv("GRAPHDEV_WORKSPACE_ROOT", r"F:\Upwork")).resolve()

    if require_secrets:
        missing = []
        if not discord_token:
            missing.append("DISCORD_TOKEN")
        if not openai_api_key:
            missing.append("OPENAI_API_KEY")
        if missing:
            names = ", ".join(missing)
            raise RuntimeError(f"Missing required environment variable(s): {names}")

    workspace_root.mkdir(parents=True, exist_ok=True)

    return Settings(
        discord_token=discord_token,
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        workspace_root=workspace_root,
    )
