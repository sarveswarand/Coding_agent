import * as vscode from 'vscode';
import { ChatProvider } from './chatProvider';

export function activate(ctx: vscode.ExtensionContext) {
  ctx.subscriptions.push(
    vscode.window.registerWebviewViewProvider('orgai.chat', new ChatProvider(ctx))
  );
}

export function deactivate() {}
