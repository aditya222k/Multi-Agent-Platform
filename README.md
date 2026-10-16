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

For a custom API, the JSON template may use `{{model}}`, `{{system}}`, `{{prompt}}`, `{{role}}`, `{{assignment}}`, and `{{api_key}}`. The response path uses dots and numeric array indexes, for example `choices.0.message.content`. This covers HTTP JSON model APIs that accept a single request and return JSON text. Streaming, OAuth flows, and provider-specific tool calls are not yet supported.

## Use group chats

Open **Group chats**, create or select a group, and rename it as you like. Switch bots on or off in the right panel. Use the arrows to arrange the order in which they speak. Send a message to start one round: each bot gets the prior chat and the responses from earlier bots in that round. The chat history is saved locally and is available after a restart.

## Desktop files

- Only individual files inside your Desktop can be allowed. Outside paths, folders, and symlinks pointing outside Desktop are rejected.
- Allowed files are not attached automatically. Select them for a specific message; the selection clears after sending. The selected file text is read once and sent to the API endpoints of the bots in that group.
- Files are read-only UTF-8 snapshots. The app cannot edit files. Limits: 128 KB per file, 512 KB total, 20 files per message.

The server listens only on `127.0.0.1` and requires a random launch token. Bot settings, group history, and allowed file paths are stored in `.local/settings.json` with owner-only permissions and ignored by Git. API keys stay in server memory only and must be re-entered after restart.

## Test

```sh
python3 -m unittest discover -s tests -v
```

Tests use local fakes and do not call a paid model API.
