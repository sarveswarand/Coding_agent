import * as vscode from 'vscode';
import { ChatProvider } from './chatProvider';

export function activate(ctx: vscode.ExtensionContext) {
  ctx.subscriptions.push(
    // Keep the chat alive when the sidebar is hidden so streamed events
    // (e.g. approval cards) aren't dropped while the user looks elsewhere.
    vscode.window.registerWebviewViewProvider('orgai.chat', new ChatProvider(ctx), {
      webviewOptions: { retainContextWhenHidden: true },
    })
  );
}

export function deactivate() {}
