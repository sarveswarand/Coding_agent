# OrgAI Code Agent

OrgAI is an AI coding agent that runs inside Visual Studio Code, powered by a local Python FastAPI backend. It allows you to chat with an AI assistant that can inspect, search, and edit files, as well as run shell commands in your workspace with human-in-the-loop approvals.

---

## Architecture

The project consists of two main components:

1. **VS Code Extension (`vscode-extension/`)**:
   - Written in TypeScript.
   - Contributes a custom Activity Bar view and chat webview (`ChatProvider`).
   - Communicates with the FastAPI backend via HTTP (Server-Sent Events for streaming chat responses and REST requests for tool approvals).

2. **Python FastAPI Backend (`agent-backend/`)**:
   - Built with FastAPI (`app/main.py`).
   - Orchestrates LLM provider interactions and iterative tool calling loops (`app/orchestrator.py`).
   - Provides safe file operations, searching (`app/tools.py`), and model gateway integrations (`app/gateway.py`).

---

## Agent Tools

The agent has access to the following workspace tools:
- **`read_file`**: Read a text file with line numbers, optionally just a line range (`start_line`/`end_line`).
- **`list_dir`**: List files and directories in the workspace.
- **`find_files`**: Find files by name with a glob such as `*.py` or `src/**/*.ts`.
- **`search`**: Search file contents for a regex or plain text, optionally limited to files matching a glob and with context lines (uses `ripgrep` if available, or falls back to recursive Python file search).
- **`git_status`** / **`git_diff`**: Show the branch, the changed files, and uncommitted changes.
- **`edit_file`**: Edit a file by replacing exact `old` text with `new` text (or create a new file if `old` is empty). Requires user approval unless edits are auto-approved (see below).
- **`run_command`**: Execute a shell command in the workspace root. Requires user approval unless the command is allowed (see below).

### What the agent is told

- **Editor context**: each message includes the active file, your selection, the cursor line and that file's errors and warnings. Turn this off with the `orgai.sendEditorContext` setting.
- **Project instructions**: if the workspace root contains `AGENTS.md` or `.orgai.md`, its contents are added to the agent's instructions. This is a good place for build and test commands and coding conventions.
- **Environment**: the OS, the shell `run_command` uses (cmd.exe on Windows), the workspace root and the git branch.

### Approval settings

- `orgai.approvalMode`: `ask` (default) asks before every edit and command. `autoEdits` applies edits without asking (each diff is still shown in the chat) and still asks before commands.
- `orgai.allowedCommands`: commands that run without asking, for example `["npm test", "git status"]`. An entry also allows extra arguments (`npm test -- --watch=false`). Commands containing `&`, `|`, `;`, `<`, `>`, `` ` ``, `$(` or a line break always ask.
- **Always allow** on a command's approval card runs that exact command without asking for the rest of the chat.

These two settings can only be set in your user settings, not in a repository's `.vscode/settings.json`, so a cloned repository can't approve its own commands.

---

## Setup and Running the Backend

1. Navigate to the backend directory:
   ```bash
   cd agent-backend
   ```

2. Create and activate a Python virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Configure environment variables (an API key for Anthropic, Gemini, or both):
   ```bash
   cp .env.example .env
   # Edit .env and add your API keys
   ```
   **Using Gemini's free tier:** create a key at https://aistudio.google.com/apikey, set `GEMINI_API_KEY` in `.env`, and leave `ANTHROPIC_API_KEY` empty. The default model is `gemini-3.4-flash-lite` (change it with `GEMINI_MODEL`). Free-tier limits are per minute and per day (see https://aistudio.google.com/rate-limit). When the per-minute limit is hit, the agent waits as long as Gemini asks and then continues; the daily limit resets at midnight Pacific time. Google may use free-tier prompts and responses, which include your code, to improve its products (see the [Gemini API terms](https://ai.google.dev/gemini-api/terms)).

5. Start the FastAPI development server:
   ```bash
   uvicorn app.main:app --reload --port 8765
   ```

---

## Running and Installing the VS Code Extension

1. Navigate to the extension directory:
   ```bash
   cd vscode-extension
   ```

2. Install Node dependencies:
   ```bash
   npm install
   ```

3. Compile the extension:
   ```bash
   npm run compile
   ```

4. **Running in VS Code**:
   - Open the `vscode-extension` folder in VS Code (`code vscode-extension`).
   - Press `F5` to open a new **Extension Development Host** window.
   - Open any workspace folder in the development window and click the **OrgAI** icon in the Activity Bar to start chatting.

5. **Installing the pre-built VSIX**:
   - You can install `vscode-extension/orgai-code-agent-0.0.1.vsix` directly in VS Code via the Extensions view menu (`...` -> "Install from VSIX...").

---

## Voice app for your phone

The backend serves a web app at `/app/` that you can open on your phone (and add to the home screen). Tap **Talk** and speak: the agent narrates what it's doing, asks you to approve edits and commands by voice (or buttons), and you can speak again while it works to redirect it.

1. **Pair** (on the PC, in `agent-backend/`):
   ```bash
   python -m app.pair --workspace "C:\Users\you\projects" --url https://YOUR-PC.your-tailnet.ts.net
   ```
   This creates `AGENT_TOKEN` in `.env`, limits the phone to the folder(s) you list (`AGENT_WORKSPACES`) and prints a link with the token in it. Restart the backend afterwards.
2. **Reach the PC from your phone** without opening a port to the internet: install [Tailscale](https://tailscale.com) on both, then run `tailscale serve --bg 8000` on the PC. That gives an `https://` address, which phones require for microphone access.
3. Open the printed link on the phone. Pick a project folder and tap **Talk**.

Security notes: `/v1/*` always needs the token and only works inside `AGENT_WORKSPACES`. The VS Code endpoints stay open for direct local connections only; anything arriving through a proxy such as `tailscale serve` is treated as remote and needs the token. Anyone holding the token can run commands on the PC, so keep the link private; `python -m app.pair --new-token` revokes it. Voice recognition uses the phone browser's built-in speech service (Chrome on Android works best; on iPhone use the keyboard's dictation button).

### Claude Code mode

In the phone app's settings, **Agent** can be *Claude Code (on the PC)*. The backend then runs your installed Claude Code (`claude -p`, found on PATH, via `CLAUDE_BIN`, or in the VS Code extension folder) in the chosen project and relays what it does: narration, tool activity, and approval prompts that you answer on the phone. It uses your Claude Code login; `ANTHROPIC_API_KEY` is never passed to it. Conversations continue across messages (ids are kept in `agent-backend/.cli_sessions.json`). Stop ends the process and anything it started.

### Voice commands and the local model

The phone's voice layer never uses a cloud API key: its own thinking (status summaries) and the built-in agent run on a local Ollama model (`LOCAL_MODEL` in `.env`, else the first installed one; a small model such as `qwen3:1.7b` or `llama3.2:3b` is plenty and much faster on CPU). The server handles a few spoken commands itself and sends everything else to the agent as a prompt:

- **"open VS Code"** / **"open the MyApp folder"** / **"open MyApp"**: opens VS Code, on that project if you name one. Names are matched loosely ("coding agent" finds `Coding_Agent`); if two projects fit, it asks which.
- **"open Claude Code"** (optionally "...in ai recommendation"): opens the project in VS Code and shows the Claude Code panel, on the same conversation the voice agent is using.
- **"switch to MyApp"** / **"use MyApp"**: makes it the active project (each project gets its own conversation). It won't switch while the agent is working.
- **"list my projects"**: reads out the projects it can see.
- **"what's happening?"** (also "status", "are you done?"): a short spoken update, even mid-run.
- **"stop"**: cancels the run. **"yes"/"no"**: answers an approval.

- **"open Chrome"**, **"open Notepad"**, **"open the calculator"** and other apps on your list: opens them straight away. The list is short and fixed (found on this PC, plus anything you add in `agent-backend/apps.json`; see `apps.example.json`). A name that isn't on it is treated as a prompt for the agent, never launched.
- **Risky things ask first**: **"close Chrome"**, **"lock my PC"**, **"shut down my PC"**, **"restart my PC"**. The assistant asks, and only a spoken or typed "yes" within a minute does it ("no", or saying anything else, drops it). Shutdown and restart wait 30 seconds, and **"cancel shutdown"** stops them. Closing is polite (apps can ask to save first).

- **"open my inbox"**, **"open my Gmail inbox in Chrome"**, **"go to YouTube"**, **"open github dot com"**: opens the website in a new Chrome tab (Edge if you say so) instead of asking the agent, which can't drive a browser. Say **"in the same tab"** and, if the browser is in front, it types the address into that tab's address bar instead. "Inbox" means Gmail; add your own sites or change that in `agent-backend/sites.json` (`{"sites": {"outlook": {"label": "Outlook", "url": "https://outlook.office.com/mail/", "names": ["outlook", "inbox"]}}}`).
- **"search for Sundar Pichai"**, **"google X"**, **"look up the weather in Chennai"**: searches Google in Chrome (or Edge if you say so) instead of asking the agent. A bare "search for ..." only goes to the browser when it's short and doesn't look about code; "search for where the token is validated" or "search the codebase for TODOs" stay prompts for the agent. Say "on Google", "on the web" or "in Chrome" to force the browser.
- **"what's on my screen?"** (also "look at my screen", "what do you see"): takes a screenshot of the main screen and a local vision model (`VISION_MODEL` in `.env`, else the first installed one such as `qwen2.5vl:3b`; `ollama pull qwen2.5vl:3b`) describes it. The picture stays in memory and goes only to Ollama on this PC. It answers "Looking at your screen" at once and speaks the description when ready: about 10 seconds once the model is loaded, but around 2 minutes the first time after it has been idle (it loads from disk on CPU). It can misread small text and doesn't see other monitors.
- **"type Sundar Pichai"** / **"press enter"** (also tab, escape, backspace): sends keystrokes to the window in front, but only if it's a browser or Notepad (`VOICE_TYPE_APPS` in `.env` changes the list), never a terminal or editor. It can't see the result; use "what's on my screen?" to check. Windows ignores keystrokes aimed at an admin (elevated) window.
- **"open MySQL Workbench"** (or any app with a Start Menu shortcut): opens it by name; only "open"/"launch" and only when the whole request is the app's name. Apps outside the short list in `apps.json` are never closed from here.
- **Loosely worded requests** ("pull up the browser thing", "get the calculator going", "get me into the my app repo"): when no fixed phrase matches a short sentence that sounds like a command, the local model suggests an action and the assistant asks **"Did you mean: open Chrome? Say yes."** first. A suggestion only counts if your own words name that app or project (so "open the login page" can't turn into opening some folder), and anything doubtful stays a prompt for the agent. Shutdown and restart are never chosen by the model. It costs about 2 seconds when it's consulted, and only short, command-sounding sentences are sent to it.

Projects are the folders directly inside the ones you allowed with `pair --workspace`; nothing outside them can be opened. New commands are small functions registered with `@action` in `app/assistant.py`.

Limit: VS Code only lets other programs open the Claude Code panel (and optionally prefill a prompt). It can't be typed into or read from outside, so prompts spoken to the phone run through the Claude Code CLI on that same conversation, and the panel shows that conversation when opened.

### Locked screen

The assistant can lock the PC but not unlock it, on purpose: Windows doesn't deliver synthetic keystrokes to the lock screen, and a password spoken or typed through a chat would pass through speech recognition and the logs. Unlock by being at the PC, or with Windows' own tools (a Remote Desktop session over Tailscale, or Dynamic Lock with a paired phone). While locked, the server keeps running, but screen and typing commands only reach the lock screen.
