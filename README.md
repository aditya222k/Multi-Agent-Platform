# Local Agent Studio

A local platform with two screens: **Bot setup** and **Group chats**. Create as many bots and named groups as you need. Each bot has its own role, assignment, model connection, and API key. Add a bot to one or more groups from its settings, or switch membership on the group chat screen. When you send a message, the group's bots respond in the order shown, and each bot sees the conversation so far.

## Start

Requires Python 3.10 or newer. No packages need to be installed.

```sh
cd ~/Desktop/Multi-Agent-Platform
python3 app.py
```

On macOS you can also double-click **start.command**. The app opens in a browser. Keep the terminal running; press **Ctrl+C** to stop. If port 8765 is busy, use `python3 app.py --port 8766`.

## Set up a bot

1. Open **Bot setup** and select **New bot**.
2. Add its name, role, and assignment.
3. Enter the provider's HTTPS JSON API endpoint, model ID, and API format. The app supports **Chat completions**, **Responses**, **Messages**, and a **Custom JSON** template with a configurable response text path. Local HTTP endpoints are also allowed for models running on your computer.
4. Set the API key header and prefix expected by that endpoint. Enter any additional required headers as a JSON object. Paste the key and choose **Set key**.
5. Save the bot. Use the switches under **Group chats** to choose its rooms.

A saved assignment is the bot's task instruction: its replies should focus on that work. General assistance is enabled only when the assignment is empty. The role describes how the bot works and does not create a task by itself. Each turn identifies the current bot and next scheduled participant, with separately labelled conversation history to help keep bot identities distinct. These instructions guide the model; they do not guarantee that every model will follow them perfectly.

For a custom API, the JSON template may use `{{model}}`, `{{system}}`, `{{prompt}}`, `{{role}}`, `{{assignment}}`, and `{{api_key}}`. Include `{{system}}` or `{{prompt}}` so the bot receives its instructions. If `{{system}}` is absent, the instructions are prepended to `{{prompt}}`. The response path uses dots and numeric array indexes, for example `choices.0.message.content`. This covers HTTP JSON model APIs that accept a single request and return JSON text. Streaming, OAuth flows, and provider-specific tool calls are not yet supported.

## Use group chats

Groups offer **Task workflow** and **Group conversation** modes under **Run settings**. New groups start in Task workflow mode; existing groups retain their conversation mode.

For **Task workflow**, choose a **Reviewing master** from the group's bots. The other bots are workers, and the arrows set their task order. Give workers their tasks in Bot setup and optionally add group-wide **Quality criteria**. Press **Start workflow** with your overall goal. The current worker submits work, the master reviews its quality, and the worker separately confirms completion. Both decisions must refer to the same submitted result before the app hands off to the next worker. Rejected or unconfirmed work is revised and reviewed again. Every next worker receives **all previously approved results**. The workflow ends when every worker's task has both approvals, when you stop it, or when an error or missing input requires attention.

The app validates explicit decisions and result versions rather than inferring agreement from conversational text. Model-based review is still a judgment and does not guarantee factual accuracy. Invalid decision replies are retried up to three times, then the workflow stops without making the handoff. Progress and approved outputs are saved locally; starting a new workflow begins a fresh task sequence. This remains a text-work platform with read-only approved file snapshots, not a tool-execution environment.

Open **Group chats**, create or select a group, and rename it as you like. Switch bots on or off in the right panel. Use the arrows to arrange the order in which they speak. The chat history is saved locally and is available after a restart.

Enter your starting message and press **Start conversation**. At least two bots must join the group. They keep taking turns in the displayed order, each receiving the conversation so far and building on the preceding replies. There is no duration to choose and no need to keep sending “continue.” Press **Stop conversation** to end the run. Rate limits are retried while you can still stop the conversation; other provider errors end the run and show an error. Requests already sent to a provider may still finish (and may be billed by that provider), but replies received after stopping are discarded. Changing bot settings or membership during a run applies to the next run.

## Desktop files

- Only individual files inside your Desktop can be allowed. Outside paths, folders, and symlinks pointing outside Desktop are rejected.
- Allowed files are not attached automatically. Select them for a specific message; the selection clears after sending. The selected file text is read once and sent to the API endpoints of the bots in that group.
- Files are read-only UTF-8 snapshots. The app cannot edit files. Limits: 128 KB per file, 512 KB total, 20 files per message.

The server listens only on `127.0.0.1` and requires a random launch token. Bot settings, group history, and allowed file paths are stored in `.local/settings.json` with owner-only permissions and ignored by Git. API keys stay in server memory only and must be re-entered after restart.

HTTPS connections verify server certificates. On macOS, if Python has no default trusted certificates, the app uses `/etc/ssl/cert.pem`. Explicit `SSL_CERT_FILE` or `SSL_CERT_DIR` settings take precedence.

## Test

```sh
python3 -m unittest discover -s tests -v
```

Tests use local fakes and do not call a paid model API.
