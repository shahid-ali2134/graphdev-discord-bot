# GraphDev LangGraph Discord Agent

GraphDev is a conversational Discord development assistant built with Python,
LangGraph, LangChain, OpenAI, and discord.py. It lets Discord users inspect
projects, create or edit files, save attachments, install dependencies, run
Python scripts, execute notebooks, and recover from backups inside a constrained
workspace.

Example messages:

```text
!graphdev open the landing page folder and add a blue button
!graphdev inspect this project and explain how it is structured
!graphdev create a LangGraph workflow project for invoice triage
!graphdev run that script again
!graphdev save these attachments into the active project
```

## Project Structure

```text
.
├── main.py             Discord client, command parsing, runtime checks
├── graph.py            LangGraph state machine and OpenAI tool-calling agent
├── agent_tools.py      LangChain tools exposed to the agent
├── workspace.py        Safe filesystem, backup, execution, and install helpers
├── stores.py           Per-user memory and pending approval persistence
├── config.py           Environment loading and runtime settings
├── requirements.txt    Python dependencies
└── README.md           Project documentation
```

The bot stores runtime state in a local `.graphdev` folder:

- `.graphdev/memory.json` tracks each Discord user's active project, active
  folder, recent paths, and compact conversation summary.
- `.graphdev/pending_actions.json` tracks the one approval-gated action waiting
  for a user's `YES` or `NO` response.

## AI Agent

The AI agent is a custom LangGraph workflow implemented in `graph.py`. It uses
LangChain's `ChatOpenAI` chat model with tool binding:

```python
ChatOpenAI(
    model=settings.openai_model,
    api_key=settings.openai_api_key,
    temperature=0.2,
).bind_tools(tools)
```

The model is configured with the `OPENAI_MODEL` environment variable. If that
variable is not set, the bot defaults to:

```env
OPENAI_MODEL=gpt-4o-mini
```

So the default AI agent is an OpenAI `gpt-4o-mini` tool-calling chat model
orchestrated by a custom LangGraph state graph. It is not using a hosted
OpenAI Assistant or a prebuilt LangGraph agent object; the repo defines the
state, nodes, routes, tools, memory, and approval flow directly.

## How It Works

The runtime starts in `main.py`:

1. The Discord client listens for messages that start with `!graphdev`, mention
   the bot, or reply `YES` / `NO` to a pending approval.
2. Attachments are converted into metadata payloads with filename, URL, content
   type, and size.
3. The request is passed to `GraphDevAgent.invoke()` in a worker thread so the
   Discord event loop stays responsive.
4. `GraphDevAgent` builds a LangGraph app for the user, loads memory and any
   pending action, and then routes the request.
5. The LLM reads the system prompt, user memory, attachment summary, workspace
   root, and current message.
6. If the LLM needs project context, it calls read-only tools such as
   `scan_project_tool`, `read_file_tool`, or `profile_attachment_tool`.
7. If the LLM needs to write, modify, delete, install, execute, save
   attachments, or roll back files, the matching tool queues an approval action
   instead of doing the risky operation immediately.
8. When the user replies `YES`, the stored action is executed and the original
   request continues from the next unfinished step.
9. The final assistant response is sent back to Discord and summarized into
   per-user memory.

## Running Workflows

The LangGraph workflow in `graph.py` has these nodes:

```text
START
  -> load_context
      -> approval   when a pending action exists
      -> agent      when no approval is pending

agent
  -> tools          when the OpenAI model requests a tool call
  -> final          when the model answers directly

tools
  -> agent          after tool output is added to the conversation

approval
  -> END            when the user cancels, gives an invalid approval reply,
                    or the approved action finishes without continuation
  -> agent          when the approved action finishes and the original request
                    should continue

final
  -> END
```

The main workflows available to users are:

- Project resolution and inspection:
  `resolve_project_tool`, `scan_project_tool`, `read_file_tool`,
  `profile_attachment_tool`.
- Planning:
  `generate_project_plan_tool`, `apply_project_plan_tool`.
- File and project changes:
  `create_project_tool`, `write_file_tool`, `modify_file_tool`,
  `delete_path_tool`.
- Execution:
  `run_file_tool` for Python scripts and `run_notebook_tool` for Jupyter
  notebooks.
- Dependency installation:
  `install_dependencies_tool`, which creates or reuses a project `.venv` and
  installs packages with pip.
- Attachment handling:
  `save_attachment_tool`, which downloads or copies Discord attachments into
  the workspace after approval.
- Recovery:
  `create_backup_tool` and `rollback_tool`.

## Safety Model

All runtime file operations are constrained to the configured workspace root:

```text
F:\Upwork
```

You can override it with:

```env
GRAPHDEV_WORKSPACE_ROOT=F:\Upwork
```

The bot queues approval before writing, modifying, deleting, installing
dependencies, executing scripts, executing notebooks, saving attachments, or
rolling back backups. Reply `YES` to approve or `NO` to cancel.

Existing files are backed up under:

```text
F:\Upwork\.graphdev_backups
```

Secret files such as `.env` are never printed, and the tools refuse to modify
secret files or `.git` paths.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
New-Item -ItemType File .env
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
