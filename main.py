import argparse
import asyncio
import socket
from pathlib import Path

import aiohttp
import discord

from config import Settings, load_settings
from graph import GraphDevAgent, build_graphdev_agent


processing_users: set[int] = set()


def split_message(text: str, max_length: int = 1900) -> list[str]:
    if not text:
        return [""]

    chunks = []
    remaining = str(text)
    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = remaining.rfind(" ", 0, max_length)
        if split_at == -1:
            split_at = max_length

        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()

    if remaining:
        chunks.append(remaining)
    return chunks


def strip_command_prefix(content: str, settings: Settings) -> str:
    content = content.strip()
    if content.lower().startswith(settings.command_prefix):
        return content[len(settings.command_prefix):].strip()
    return content


def strip_bot_mention(content: str, client: discord.Client) -> str:
    if not client.user:
        return content.strip()

    content = content.replace(f"<@{client.user.id}>", "")
    content = content.replace(f"<@!{client.user.id}>", "")
    return content.strip()


def attachment_payloads(message: discord.Message) -> list[dict]:
    return [
        {
            "filename": attachment.filename,
            "url": attachment.url,
            "content_type": attachment.content_type,
            "size": attachment.size,
        }
        for attachment in message.attachments
    ]


async def send_long_message(channel: discord.abc.Messageable, text: str, settings: Settings) -> None:
    for chunk in split_message(text, settings.max_discord_message_length):
        await channel.send(chunk)


def create_client(settings: Settings, agent: GraphDevAgent) -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready() -> None:
        print(f"Logged in as {client.user}")
        print(f"Workspace root: {settings.workspace_root}")
        print(f"Listening for '{settings.command_prefix}' or direct mentions.")

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author.bot:
            return

        raw_content = message.content.strip()
        has_prefix = raw_content.lower().startswith(settings.command_prefix)
        mentioned = client.user in message.mentions if client.user else False
        is_approval = raw_content.upper() in {"YES", "NO"}

        if not raw_content and not message.attachments:
            return
        if not has_prefix and not mentioned and not is_approval:
            return

        user_text = raw_content
        if not is_approval:
            user_text = strip_command_prefix(raw_content, settings)
            if mentioned:
                user_text = strip_bot_mention(user_text, client)

        if not user_text and message.attachments:
            user_text = "Save and inspect these attachments."
        elif not user_text:
            await message.channel.send(f"Send a message after `{settings.command_prefix}` or mention me naturally.")
            return

        user_id = str(message.author.id)
        if message.author.id in processing_users:
            await message.channel.send("I am still working on your previous request. Please wait until it finishes.")
            return

        processing_users.add(message.author.id)

        try:
            async with message.channel.typing():
                response = await asyncio.to_thread(agent.invoke, user_id, user_text, attachment_payloads(message))
            await send_long_message(message.channel, response, settings)
        except Exception as error:
            await message.channel.send(f"Something went wrong: `{error}`")
        finally:
            processing_users.discard(message.author.id)

    return client


def check_runtime() -> None:
    settings = load_settings(require_secrets=False)
    build_graphdev_agent(settings)._build_graph("health-check", [])
    print("Runtime check passed: settings loaded, workspace root exists, and LangGraph compiled.")


def validate_secret_shapes(settings: Settings) -> None:
    if settings.discord_token.count(".") != 2:
        print("Warning: DISCORD_TOKEN does not look like a standard Discord bot token.")
    if not settings.openai_api_key.startswith(("sk-", "sk-proj-")):
        print("Warning: OPENAI_API_KEY does not look like a standard OpenAI API key.")


def print_connection_help(error: Exception) -> None:
    print("\nCould not connect to Discord.")
    print("The bot failed while resolving or connecting to discord.com:443.")
    print("This usually means DNS, internet connectivity, VPN/proxy, firewall, or Discord outage issues.")
    print("\nTry these checks in PowerShell:")
    print("  Resolve-DnsName discord.com")
    print("  Test-NetConnection discord.com -Port 443")
    print("  nslookup discord.com 8.8.8.8")
    print("\nIf those fail, fix DNS/network access first. If they pass, check proxy/firewall/antivirus rules for Python.")
    print(f"\nOriginal error: {error}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the GraphDev Discord agent.")
    parser.add_argument("--check", action="store_true", help="Validate imports/configuration without connecting to Discord.")
    args = parser.parse_args()

    if args.check:
        check_runtime()
        return

    settings = load_settings()
    validate_secret_shapes(settings)
    agent = build_graphdev_agent(settings)
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    Path(".graphdev").mkdir(exist_ok=True)
    try:
        create_client(settings, agent).run(settings.discord_token)
    except (aiohttp.ClientConnectorDNSError, aiohttp.ClientConnectorError, socket.gaierror) as error:
        print_connection_help(error)


if __name__ == "__main__":
    main()
