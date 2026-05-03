# GraphDev LangGraph Discord Agent

GraphDev is a conversational Discord development assistant built with Python,
LangGraph, LangChain, OpenAI, and discord.py.

It understands natural development requests such as:

```text
!graphdev open the landing page folder and add a blue button
!graphdev inspect this project and explain how it is structured
!graphdev create a LangGraph workflow project for invoice triage
!graphdev run that script again
!graphdev save these attachments into the active project
```

## Safety Model

All runtime file operations are constrained to:

```text
F:\Upwork
```

The bot queues approval before writing, modifying, deleting, installing
dependencies, executing scripts, executing notebooks, saving attachments, or
rolling back backups. Reply `YES` to approve or `NO` to cancel.

Existing files are backed up under:

```text
F:\Upwork\.graphdev_backups
```

Secret files such as `.env` are never printed.

## Architecture

```text
Discord message
-> conversational LangGraph state
-> memory/context loader
-> LLM planner/tool selector
-> reusable workspace tools
-> approval gate
-> tool execution
-> final response
```

Core files:

- `main.py` - Discord event loop and UX
- `graph.py` - LangGraph agent orchestration
- `agent_tools.py` - reusable LangGraph tools
- `workspace.py` - path-safe workspace operations
- `stores.py` - per-user memory and pending approvals
- `config.py` - environment and settings

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Fill in `.env`:

```env
DISCORD_TOKEN=your_discord_bot_token_here
OPENAI_API_KEY=your_openai_api_key_here
OPENAI_MODEL=gpt-4o-mini
GRAPHDEV_WORKSPACE_ROOT=F:\Upwork
```

Enable Message Content Intent in the Discord Developer Portal, then run:

```powershell
python main.py --check
python main.py
```

## Discord Testing

In a Discord channel where the bot is present:

```text
!graphdev what projects can you see?
!graphdev create a small Python CLI project named hello-cli
YES
!graphdev inspect hello-cli
!graphdev add a README with setup instructions
YES
!graphdev run the main Python file
YES
```

You can also mention the bot instead of using the prefix.
