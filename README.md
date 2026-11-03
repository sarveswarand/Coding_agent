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
