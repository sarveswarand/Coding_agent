import * as vscode from 'vscode';
import { randomUUID } from 'crypto';

export class ChatProvider implements vscode.WebviewViewProvider {
  private sessionId = randomUUID();
  private current?: AbortController; // the in-flight /chat request, if any
  constructor(private ctx: vscode.ExtensionContext) {}

  resolveWebviewView(view: vscode.WebviewView) {
    const mediaRoot = vscode.Uri.joinPath(this.ctx.extensionUri, 'media');
    view.webview.options = { enableScripts: true, localResourceRoots: [mediaRoot] };
    view.webview.html = this.html(view.webview, mediaRoot);

    const base = () => vscode.workspace.getConfiguration('orgai').get<string>('backendUrl')!;
    const post = (body: unknown) => ({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    view.webview.onDidReceiveMessage(async (m) => {
      if (m.type === 'ready') {
        try {
          const res = await fetch(`${base()}/models`);
          view.webview.postMessage({ type: 'models', models: await res.json() });
        } catch (e: any) {
          view.webview.postMessage({ type: 'error', message: `Backend unreachable: ${e.message}` });
        }
      }
      if (m.type === 'newChat') {
        this.sessionId = randomUUID();
      }
      if (m.type === 'stop') {
        // Ask the backend to cancel; it replies with a 'stopped' event and closes
        // the stream. If it doesn't, drop the connection ourselves after 3s.
        const ac = this.current;
        const sid = this.sessionId;
        try {
          await fetch(`${base()}/cancel`, post({ session_id: sid }));
        } catch { /* backend gone: the abort below ends things */ }
        setTimeout(() => ac?.abort(), 3000);
      }
      if (m.type === 'copy') {
        await vscode.env.clipboard.writeText(m.text);
      }
      if (m.type === 'approve') {
        await fetch(`${base()}/approve`, post({ id: m.id, approved: m.approved }));
      }
      if (m.type === 'send') {
        const ws = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
        if (!ws) {
          view.webview.postMessage({ type: 'error', message: 'Open a folder first.' });
          return;
        }
        const ac = new AbortController();
        this.current = ac;
        try {
          const res = await fetch(`${base()}/chat`, {
            ...post({ session_id: this.sessionId, workspace: ws, message: m.text, model: m.model }),
            signal: ac.signal,
          });
          const reader = res.body!.getReader();
          const dec = new TextDecoder();
          let buf = '';
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let i;
            while ((i = buf.indexOf('\n\n')) >= 0) {
              const chunk = buf.slice(0, i);
              buf = buf.slice(i + 2);
              if (chunk.startsWith('data: ')) {
                const ev = JSON.parse(chunk.slice(6));
                view.webview.postMessage(ev);
                // Safety net: if the panel isn't visible the user can't see the
                // card, so ask with a normal VS Code notification as well.
                if (ev.type === 'approval_request' && !view.visible) {
                  void this.askViaNotification(view, ev, base(), post);
                }
              }
            }
          }
        } catch (e: any) {
          if (ac.signal.aborted) {
            view.webview.postMessage({ type: 'stopped' });
          } else {
            view.webview.postMessage({ type: 'error', message: `Backend unreachable: ${e.message}` });
          }
        } finally {
          if (this.current === ac) this.current = undefined;
          view.webview.postMessage({ type: 'stream_end' });
        }
      }
    });
  }

  private async askViaNotification(
    view: vscode.WebviewView,
    ev: { id: string; name: string; preview?: string },
    baseUrl: string,
    post: (body: unknown) => RequestInit
  ) {
    const what = ev.name === 'edit_file' ? 'edit a file' : `run: ${(ev.preview ?? '').slice(0, 120)}`;
    const choice = await vscode.window.showInformationMessage(
      `OrgAI wants to ${what}. Open the OrgAI panel to review.`,
      'Approve',
      'Reject'
    );
    if (!choice) return; // dismissed: the card in the panel can still be answered
    const approved = choice === 'Approve';
    await fetch(`${baseUrl}/approve`, post({ id: ev.id, approved }));
    view.webview.postMessage({ type: 'approval_answered', id: ev.id, approved });
  }

  private html(webview: vscode.Webview, mediaRoot: vscode.Uri) {
    const uri = (f: string) => webview.asWebviewUri(vscode.Uri.joinPath(mediaRoot, f));
    const nonce = randomUUID().replace(/-/g, '');
    const csp = [
      `default-src 'none'`,
      `style-src ${webview.cspSource}`,
      `script-src 'nonce-${nonce}'`,
    ].join('; ');
    return /* html */ `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link rel="stylesheet" href="${uri('chat.css')}">
</head>
<body>
  <header>
    <div class="brand"><span id="status" class="dot" title="Connecting..."></span>OrgAI</div>
    <select id="model" title="Model"><option value="">Loading...</option></select>
    <button id="new" class="icon-btn" title="New chat">+ New</button>
  </header>

  <main id="log">
    <section id="empty">
      <div class="logo">AI</div>
      <h2>How can I help with your code?</h2>
      <p>I can explore, edit and run commands in this workspace. Changes always need your approval.</p>
      <div class="chips">
        <button class="chip" data-prompt="List the top-level files and explain what this project does.">Explain this project</button>
        <button class="chip" data-prompt="Find potential bugs or risky code in this project and suggest fixes.">Review the code for bugs</button>
        <button class="chip" data-prompt="Create a README.md for this project based on the code.">Write a README</button>
      </div>
    </section>
  </main>

  <footer>
    <div class="composer">
      <textarea id="input" rows="1" placeholder="Ask about your code..."></textarea>
      <button id="send" title="Send (Enter)" aria-label="Send">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor"><path d="M1.2 1.6a.6.6 0 0 1 .8-.5l12.6 6.4a.6.6 0 0 1 0 1L2 14.9a.6.6 0 0 1-.8-.7L3 8.2 1.2 2.3a.6.6 0 0 1 0-.7zM4.2 8.6l-1.3 4.3L12.3 8 2.9 3.1l1.3 4.3h5.6v1.2H4.2z"/></svg>
      </button>
    </div>
    <div id="usage" class="usage" hidden></div>
    <div class="hint">Enter to send · Shift+Enter for a new line</div>
  </footer>

  <script nonce="${nonce}" src="${uri('chat.js')}"></script>
</body>
</html>`;
  }
}
