# Running AzLeechBot on Wispbyte

Wispbyte is a Pterodactyl-style panel: **it clones your GitHub repo onto the
server, then runs your startup command.** There is no image to build and
nothing to push except the Git repository.

The repo already contains everything needed:

| file | what it is |
|---|---|
| `bot.py`, `bot_pkg/` | the bot itself |
| `requirements.txt` | python deps (tgcrypto moved out — see below) |
| `requirements-optional.txt` | tgcrypto, installed best-effort only |
| `startup.sh` | the startup command: venv + deps + missing binaries + run |
| `.env.example` | every environment variable the bot understands |

---

## 1. Create the server

1. Log in at <https://wispbyte.com/client> → **Create Server**.
2. Pick a **Python** egg. Use **Python 3.11** if the list offers it.
   - Why 3.11: pyrogram 2.x + tgcrypto wheels exist for cp311 only.
     3.12/3.13 also run (the bot creates an event loop before importing
     pyrogram), but you lose native crypto → slower MTProto uploads.
3. Name it, pick the smallest node that fits (1 GB RAM is enough for one
   bot; the queue workers are already capped for low-RAM hosts).

## 2. Point the server at the GitHub repo

Server sidebar → **GitHub** (under General) → *GitHub Integration*.

| field | value |
|---|---|
| Repository | `https://github.com/hertges/AzBot` |
| Branch | `main` |
| Username | your GitHub username (`hertges`) |
| Token | a GitHub **PAT** with `repo` scope (private repo → token required) |
| Auto-update on startup | on (so every server restart pulls your latest commit) |
| Skip install when uploading your own files | leave off |

Press **Clone and Pull**. The repo lands in the server's home directory.

> The PAT goes into the panel, not into the repo. Never commit `.env`.

## 3. Set the environment variables

Server sidebar → **Startup** → *Server Configuration* (or *Environment*).

Required:

```
API_ID=1234567
API_HASH=0123456789abcdef0123456789abcdef
BOT_TOKEN=1234567890:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Optional (leave blank to disable the feature):

```
ADMIN_ID=206136283          # numeric Telegram user id; blank = anyone can use the bot
LOG_LEVEL=INFO
```

Where the values come from:

- `API_ID` / `API_HASH` → <https://my.telegram.org/apps> (a *bot* account is
  fine; pyrogram needs MTProto credentials, that is what lifts the 50 MB
  upload limit).
- `BOT_TOKEN` → from **@BotFather** in Telegram.

Google Drive upload (optional) — add these to enable the ☁️ Drive
destination:

```
GCP_CLIENT_ID=...
GCP_CLIENT_SECRET=...
GCP_REFRESH_TOKEN=...
DRIVE_FOLDER_ID=            # optional: pin every upload in one folder
DATA_DRIVE_FOLDER=AzBotData # optional: folder holding settings.json + indexes
```

## 4. Set the startup command

Server sidebar → **Startup** → *Startup Command*:

```
sh startup.sh
```

Save (Ctrl+S). The script:

1. picks the newest Python it can find (3.11 first),
2. creates `~/venv` and installs `requirements.txt` into it,
3. installs the binaries the panel image does not ship — `ffmpeg`,
   `ffprobe`, `aria2c`, `7z` (installed as `7z`) and GNU `wget` — all as
   **static** builds in `~/bin`, so no root and no `apk` are needed,
4. installs `requirements-optional.txt` (tgcrypto) best-effort,
5. refuses to start if `API_ID` / `API_HASH` / `BOT_TOKEN` are missing,
6. runs `bot.py` and restarts it 15 s after it exits.

Re-running it is safe: downloads are cached in `~/.azbot-cache` and the venv
is reused.

## 5. Start it and watch the log

**Start** the server, then open **Console**.

A healthy start looks like this:

```
[azbot] python: Python 3.11.17
[azbot] creating venv at /home/container/venv
[azbot] dependencies installed
[azbot] optional extras installed (tgcrypto native crypto)
[azbot] ffmpeg installed
[azbot] aria2c installed
[azbot] 7z installed
[azbot] GNU wget installed
[azbot] starting bot (cwd /home/container/AzBot)
```

Then the bot's own log lines and a Telegram “bot started” message.

**If the panel shows "GitHub Unavailable"** for your server type, that egg
has no git support. Use a different egg (any *GitHub-compatible* one) or
upload the files through the panel's file manager instead — `startup.sh`
works the same way either way.

---

## Pushing an update

```bash
cd <your checkout of AzBot>
# edit, then:
GIT_ASKPASS=/path/to/askpass.sh GIT_TERMINAL_PROMPT=0 git push origin main
```

With *Auto-update on startup* enabled, restart the server and the panel
pulls the new commit before running `startup.sh`.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `FATAL: missing env vars: API_ID API_HASH BOT_TOKEN` | env vars not set or not saved | re-check step 3, then restart the server |
| `CRITICAL: API_ID / API_HASH / BOT_TOKEN missing!` | same, but the bot itself caught it | same |
| `pip install` fails with "externally-managed-environment" | PEP 668 marker | `startup.sh` already retries with `--break-system-packages`; if it still fails, check the console log at `~/.azbot-pip.log` |
| `[azbot] 7z unavailable` | wrong CPU arch | check `uname -m` in the console; x86_64 and arm64 are covered |
| uploads are slow | no tgcrypto | use Python 3.11 (native crypto) or accept pure-Python crypto |
| `/clone` command fails | busybox `wget` instead of GNU wget | `startup.sh` installs GNU wget; confirm with `wget --help \| grep mirror` in the console |
| `zip -FF` repair disabled | the `zip` binary is missing from the image | only affects repairing broken zips; the bot says so and continues |
