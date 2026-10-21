# Local Agent Studio

**Run teams of AI bots on your own machine: let them chat freely, or have them work through a task with a reviewer approving each step.**

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![Runs locally](https://img.shields.io/badge/runs-localhost%20only-lightgrey)

<!-- TODO: add a screenshot or GIF, e.g. docs/screenshot.png -->

## Features

- **Bring any model.** Each bot has its own endpoint, model, and API key. Supports Chat Completions, Responses, Messages, and custom JSON APIs, including local models.
- **Group conversations.** Bots take turns in an order you choose, each one seeing the conversation so far, until you press Stop.
- **Approval-gated task workflows.** Workers complete tasks in sequence. A reviewing master must approve each result, and the worker must confirm it, before the work is handed off.
- **Desktop file context.** Share read-only snapshots of specific Desktop files with a message.
- **Private by design.** Listens only on `127.0.0.1`, needs a launch token, and keeps API keys in memory only.
- **Zero dependencies.** Pure Python standard library plus a vanilla JS frontend.

## Quick start

Requires **Python 3.10+**. Nothing to install.

```sh
git clone https://github.com/aditya222k/Multi-Agent-Platform.git
cd Multi-Agent-Platform
python3 app.py
```

On macOS you can also double-click **`start.command`**. The app opens in your browser. Keep the terminal running and press **Ctrl+C** to stop.

If port 8765 is busy, run `python3 app.py --port 8766`.

## Setting up a bot

1. Open **Bot setup** and select **New bot**.
2. Enter a **name**, **role**, and **assignment**.
3. Enter the provider's **endpoint**, **model ID**, and **API format**.
4. Set the **API key header** and **prefix**, plus any extra headers as a JSON object. Paste the key and select **Set key**.
5. Save, then use the **Group chats** switches to choose which rooms the bot joins.

### Example configurations

| Provider | Endpoint | Format | Key header | Prefix |
|---|---|---|---|---|
| Anthropic | `https://api.anthropic.com/v1/messages` | Messages | `x-api-key` | *(none)*, plus extra header `{"anthropic-version": "2023-06-01"}` |
| OpenAI | `https://api.openai.com/v1/chat/completions` | Chat completions | `Authorization` | `Bearer ` |
| Local (e.g. Ollama) | `http://localhost:11434/v1/chat/completions` | Chat completions | `Authorization` | `Bearer ` |
| Images (OpenAI or Open WebUI) | `https://api.openai.com/v1/images/generations` | Image generation | `Authorization` | `Bearer ` |

### Image bots

Bots using the **Image generation** format turn the latest message from another participant into a picture. The image is saved under `.local/images/` and shown in the chat. Image bots join group conversations but not task workflows.

### Role vs. assignment

- The **assignment** is the bot's task. Its replies focus on that work. If the assignment is empty, the bot acts as a general assistant.
- The **role** describes *how* the bot works. It doesn't create a task by itself.

On each turn, the bot is told who it is and who speaks next, and the history is labelled by speaker so bot identities stay distinct.

### Custom JSON APIs

Templates can use `{{model}}`, `{{system}}`, `{{prompt}}`, `{{role}}`, `{{assignment}}`, and `{{api_key}}`. Include `{{system}}` or `{{prompt}}`. If `{{system}}` is missing, the instructions are prepended to `{{prompt}}`.

The response path uses dots and array indexes, for example `choices.0.message.content`.

## Group modes

Choose a mode under **Run settings**. New groups start in Task workflow mode.

### Group conversation

1. Add at least two bots to the group and arrange their order with the arrows.
2. Enter a starting message and press **Start conversation**.
3. The bots take turns, building on each other's replies, until you press **Stop conversation**.

Rate-limited requests are retried. Other provider errors end the run.

### Task workflow

```
Worker 1 submits ──► Master reviews ──► Worker confirms ──► both approve? ──► Worker 2 (receives all approved results) ──► …
       ▲                                                          │ no
       └──────────────────────── revise ◄─────────────────────────┘
```

1. Choose a **reviewing master**. The other bots become **workers**, in arrow order.
2. Give each worker its task in Bot setup. You can also add group-wide **quality criteria**.
3. Press **Start workflow** with your overall goal.

Both approvals must point to the **same submitted result** before the next worker starts. Each decision gets three attempts in total. If none returns a valid decision, the workflow stops without a handoff. Progress and approved outputs are saved. Starting a new workflow begins a fresh task sequence.

## Desktop files

- Only individual files inside your Desktop can be allowed. Paths outside Desktop, folders, and symlinks that lead outside are rejected.
- Files are not attached automatically. Select them for a specific message. The selection clears after you send it.
- Each file is read once as a read-only UTF-8 snapshot and sent to the bots in that group. The app never edits files.
- Limits: 128 KB per file, 512 KB total, 20 files per message.

## Optional: Supabase accounts

By default everything is saved on your computer and no setup is needed. To keep bots and chats in a Supabase account instead, so each person signs in and sees only their own data:

1. Create a project at [supabase.com](https://supabase.com).
2. Open **SQL Editor**, paste [`supabase/schema.sql`](supabase/schema.sql), and run it. It creates the tables, row-level security rules, and a private `images` bucket.
3. Under **Authentication → Sign In / Providers**, turn off public sign-ups, then invite each person from **Authentication → Users**.
4. Copy `.env.example` to `.env` and fill in the **Project URL** and **publishable (anon) key** from **Project Settings → API**. Never use the secret / `service_role` key.
5. Run `python3 app.py` and sign in.

Changes are saved to an outbox on this computer (`.local/outbox.db`) first, then sent to Supabase in batches every few seconds. If Supabase does not respond, nothing is lost: new changes keep collecting in the outbox and are sent, in order, as soon as it answers again, including after a restart or the next sign-in. The top bar shows **All changes saved**, **Saving…**, or **Offline · N waiting**. A change Supabase rejects outright is set aside and reported so it cannot block the rest.

If the account is empty and this computer has local data, the app offers to import it. API keys and Desktop file permissions stay on your computer either way. Delete `.env` to go back to local storage.

## Security and privacy

- The server binds to `127.0.0.1` only and requires a random launch token.
- **API keys live in server memory only.** Re-enter them after a restart.
- Without Supabase, bot settings, chat history, run history, and allowed file paths are stored in a SQLite database, `.local/app.db`, with owner-only permissions. Git ignores `.local/` and `.env`. An older `.local/settings.json` is imported once on first start and left in place as a backup.
- The server keeps only the latest 200 messages per group in memory. Older messages stay in storage and load a page at a time.
- With Supabase, bots, chats, and generated images are sent to your Supabase project, protected by row-level security. Allowed file paths stay in `.local/grants.json`.
- HTTPS certificates are verified. On macOS without default certificates, `/etc/ssl/cert.pem` is used. `SSL_CERT_FILE` and `SSL_CERT_DIR` take precedence.
- File content you share is sent to the API endpoints of the bots in that group.

## How it works

```
static/        Frontend (index.html, app.js, style.css)
app.py         HTTP server, saved state, sign-in, and background conversation runs
core.py        Bot validation, prompt building, model API calls, file access
storage.py     Local SQLite storage or a Supabase account (auth, tables, images)
workflow.py    Approval-gated task workflow engine
supabase/      Schema to run once in the Supabase SQL editor
tests/         Unit tests using local fakes
```

## Limitations

- Bot instructions guide the model but don't guarantee that every model follows them.
- Model-based review is a judgment call, not a guarantee of factual accuracy.
- Streaming, OAuth flows, and provider tool calls are not yet supported.
- Bots can't run tools or change files. Image bots can generate pictures, but no bot can read images.
- Requests already sent when you press Stop may still finish, and the provider may still bill you for them. Their replies are discarded.
- Bot or membership changes made during a run take effect on the next run.
- Up to 4 groups run at once, with at most 3 model calls in flight across all of them. Further runs wait as **Queued**. If the app stops mid-run, that run shows as **Interrupted** after a restart.

## Running tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use local fakes and never call a paid API.

## Contributing

Issues and pull requests are welcome. Please run the test suite before you submit.
