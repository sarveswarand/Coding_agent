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
- **`read_file`**: Read the contents of a text file.
- **`list_dir`**: List files and directories in the workspace.
- **`search`**: Search file contents for a regex pattern (uses `ripgrep` if available, or falls back to recursive Python file search).
- **`edit_file`**: Edit a file by replacing exact `old` text with `new` text (or create a new file if `old` is empty). Requires user approval.
- **`run_command`**: Execute a shell command in the workspace root. Requires user approval.

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

4. Configure environment variables (API keys for LLM providers like OpenAI, Anthropic, or Gemini):
   ```bash
   cp .env.example .env
   # Edit .env and add your API keys
   ```

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
