# AzLeechBot (Pyrogram rewrite)

A Telegram bot that watches for links (YouTube, Instagram, TikTok, Twitter/X,
Reddit, and hundreds of other sites via `yt-dlp`) and leeches them straight
into the chat — **no command needed**, just paste a link. Uploads go over
Telegram's MTProto layer via [Pyrogram](https://docs.pyrogram.org), not the
HTTP Bot API, so files up to Telegram's real per-file limit for bots
(~2GB) go through natively — no 50MB wall, no local Bot API server needed.

## v22 changelog — documentation audit

- **Command reference completed.** Every one of the 64 registered commands is
  now documented, plus an alias table (`/ym` `/yl` `/galleryzm` `/ddownload`
  `/dwm` … were accepted but unmentioned). `/igindex`, `/help`, `/rclone`,
  `/igtoken` and the Drive-account commands were missing entirely.
- **Phantom commands removed.** The README mentioned `/zipd`, which does not
  exist. `tests/sim_flows.py` was cited as verifying the v16 fixes — that file
  never existed in the repo.
- **Index location corrected.** The README claimed `data/indexes/<chat>/<user>.<type>.txt`,
  a Drive-only ledger, and a `data/ig_archives/` fallback. The code uses a flat
  `data/indexes/<user>.<type>.txt`, shared across chats and mirrored to
  `AzBotData/indexes/` when Drive is configured. The wrong claim was also in
  `datastore.py`'s docstring and a comment in `handlers_instagram.py`.
- **Instagram setup A/B de-conflicted.** "Why it works this way" demanded a
  Facebook Page and named the Facebook-Login permission while Setup A says no
  Page is needed. Both now describe their own setup.
- **Environment variables documented.** All 32 the code reads, grouped with
  defaults; `.env.example` gained `DISABLE_TGCRYPTO`, `TGCRYPTO_SELFTEST`,
  `GOOGLE_REDIRECT_URI` and the `TELEGRAM_TOKEN` alias note.
- **Token text fixed while documenting it.** `/directdownload` rendered
  `❌ expired — paste a new token … — -0 days left` (negative rounding plus a
  doubled dash) and said `1 days`. `ig_token.life_text()` now truncates at
  zero, pluralises, and omits the suffix when STATUS already says it.
- **Self-check section** documents the three real scripts; the `/help` section
  describes the six buttons that exist instead of seven sections that don't.

## v21 changelog — Instagram token renewal

- `bot_pkg/ig_token.py`: token store (`data/ig_token.json`, 0600), expiry
  tracking, `GET /refresh_access_token` renewal, and a watchdog that refreshes
  when fewer than 10 days remain — an Instagram Login token never lapses while
  the bot runs.
- `/igtoken <token>` (admin, private chat): validates against Meta first,
  deletes the message carrying the token, then saves it. `/igtoken refresh` and
  `/igtoken status` too.
- `/directdownload` now shows the token's remaining life to everyone, so an
  expiry is visible instead of looking like a broken bot.
- Stored token beats the env value, so a refresh survives a restart.
- Facebook Login tokens have no refresh endpoint and the code says so rather
  than failing obscurely.

## v20 changelog — Instagram DM downloads (`/directdownload`)

- `/directdownload` links a chat to the bot's Instagram account with a
  one-time code; after that, DMing an Instagram link to that account delivers
  the files to Telegram. `/gdmlinks` (same command, alternate name) shows and
  revokes the connection.
- `bot_pkg/ig_dm.py` — link store (IGSID → chat, codes hashed and single-use,
  30-minute expiry), content classifier, DM event handler, download job.
- `bot_pkg/ig_webhook.py` — stdlib webhook listener: Meta's GET verification,
  mandatory `X-Hub-Signature-256` check, 200-then-work so Meta's 5-second
  window is never missed.
- No new dependencies. No Instagram password is ever requested or stored.
- Live streams are refused with a reason (see below), not silently dropped.

## v19 changelog — public links (`/px`, `/ua`)

- `/px` — reply to a file → pixeldrain link + direct download URL.
- `/ua <url>` / `/ualist` — upload.am fetches the file server-side; no local
  disk used at all, which is what makes files larger than the VPS disk work.
- `bot_pkg/keyring.py` — shared multi-credential rotation for both hosts:
  comma/newline env lists, last-good-first ordering, quota cooldown,
  permanent drop for revoked keys, and per-service error classification so
  a per-IP limit doesn't trigger pointless rotation.
- Every failure names the host and the actual error code; when the keyless
  x0.at fallback serves a file the reply says so.
- These live on the `beta` branch. `main` is unchanged.

## v18 changelog

Aligned with WZML-X's actual implementation (source-verified, not guessed):

- **Parallel MTProto uploads (`fast_upload.py`)** — the bot-only port of
  WZML-X's tg_transfer idea: big files are striped across MULTIPLE media
  sessions via raw `upload.SaveBigFilePart`, then sent with
  `InputFileBig`. Stock pyrogram serializes every byte through ONE media
  session (4 workers, queue depth 1) — that was your speed ceiling.
  Bot-token-only, no premium/user session; anything <10MB, photos/webp,
  or ANY failure automatically falls back to the stock path, so worst
  case equals previous behavior. Tune with `UPLOAD_SESSIONS` /
  `UPLOAD_WINDOW` in `.env`.
- **yt-dlp de-pinned (WZML-X parity)**: removed our forced
  `player_client=["tv","android","ios","web"]` and the aria2c
  external-downloader hijack for YouTube. WZML-X forces neither; on 2026
  YouTube those pins produce thin format tables ("only one quality") and
  mid-download stalls. yt-dlp's own defaults handle this now. Added their
  bounded retry sleeps (3s flat) so retries don't look like hangs.
  `YT_DLP_OPTIONS` remains as escape hatch.
- **Picker UX**: a single available quality no longer opens a one-button
  menu — it downloads directly (WZML-X behavior).
- **Instagram archives**: stall watchdog raised to 10 minutes AND the
  status shows a live "waiting on Instagram (rate-limit wait)" heartbeat
  instead of freezing silently — v15-v17 killed healthy archives after
  4 quiet minutes.

## v17 changelog

Round 2 — from live runtime logs:

- **Uploads were extremely slow**: the TgCrypto self-test called function
  names that don't exist in pyrogram (`ige_encrypt`, `ctr256`), so it
  failed on EVERY host where TgCrypto was installed and the guard blocked
  perfectly healthy native crypto, forcing the pure-Python fallback
  (~2-3x slower crypto on every byte). Self-test fixed against pyrogram's
  real API (`ige256_*`, `ctr256_encrypt`). `/stats` now shows your active
  crypto mode so this can never hide again.
- **Dead buttons / commands with no answer / EditMessage timeouts**: all
  handlers ran directly inside pyrogram's tiny default thread pool
  (≈CPU-count threads). A single slow operation (YouTube format probe,
  Drive listing waiting on the upload lock) starved that pool, so new
  messages and button taps queued indefinitely. Handlers now run as
  coroutines offloading into a dedicated 16-thread pool (`HANDLER_WORKERS`)
  — the network loop and button handling stay responsive always.
- **Quality selector showed fewer options than other bots**: (a) buttons
  were deduplicated per resolution, hiding real formats — now one button
  PER format id (exact size/container/codec/fps); (b) if a probe returns
  only low resolutions while a cookie profile is active, the bot
  automatically re-probes WITHOUT cookies (cookies often pin yt-dlp to a
  basic client → 360p-only tables); (c) client list is configurable via
  `YT_PLAYER_CLIENTS` in `.env`.
- **Boot warning** `cannot access local variable 'drive'`: leftover local
  imports shadowed the module-level one in datastore (broke boot-time
  settings/cookie restore). All removed.
- **/clean or the hourly purge loop could delete RUNNING jobs' folders**
  mid-download. Purge now skips dirs owned by active jobs.
- **Stop button gave no feedback**: tapping 🛑 now flips the message to
  "Stop requested…" instantly.
- **/drive silent while listing**: replies "☁️ Opening Drive…" immediately,
  then fills in; failures show the actual error instead of silence.
- Python 3.13 compatibility: pyrogram needs an event loop at Client()
  construction; core.py now creates one when missing.

## v16 changelog

Fixes (all verified at the time by an end-to-end run against a live bot):

- **Random restarts during heavy jobs** (e.g. archives inside group topics):
  the TgCrypto segfault-guard ran *after* Pyrogram was imported, so it could
  never actually block the crashing C extension. It now runs in
  `bot_pkg/__init__.py`, guaranteed to execute before any module can import
  pyrogram/tgcrypto, with a loud warning if that order ever regresses.
- **Bare Instagram links** opened the yt-dlp quality picker (then failed):
  auto-leech checks Instagram hosts FIRST now; Instagram always goes to
  gallery-dl. `/l <insta-link>` also works again instead of NameError-ing.
- **Thumbnails missing**: source-video thumbnails are validated +
  ffmpeg-converted to real JPEGs (webp/avif/truncated downloads used to
  silently produce none). Priority: source cover > automatic frame grab
  (thumbnails are fully automatic; there is no /setthumb).
- **No quality shown**: every native video/audio upload gets a
  `🎬 WxH • tier • duration` caption; completion summaries show the file's
  real resolution/duration (IG archive summaries too).
- Quality-picker buttons crashed when tapped (`_do_picker` scoping bug).
- `/zipm` NameError (missing import); datastore NameErrors silently broke
  settings/cookie/index Drive-mirroring; "Send to Telegram" in the /drive
  browser crashed on tap; duplicate-URL guard could stick after gallery jobs;
  disk-purge race in `ensure_free`.


## Setup

1. Get `API_ID` / `API_HASH` from <https://my.telegram.org/apps> (required
   by Pyrogram even for a bot account).
2. Get `BOT_TOKEN` from [@BotFather](https://t.me/BotFather).
3. Copy `.env.example` to `.env` and fill it in.
4. `pip install -r requirements.txt`
5. `python bot.py`

Or with Docker:

```bash
docker build -t azleechbot .
docker run --env-file .env -v $(pwd)/data:/app/data azleechbot
```

Or on a Pterodactyl-style panel (Wispbyte, Pterodactyl, Pelican Panel…):
clone this repo on the server, set the env vars in the panel, and use
`sh startup.sh` as the startup command. It creates a venv, installs the
deps, and fetches the binaries the panel image lacks (ffmpeg, aria2c, 7z,
GNU wget) as static builds — no root needed. Full walkthrough:
**[WISPBYTE.md](WISPBYTE.md)**.

## Mirror vs. leech

Like the original bot, the destination is picked by which command you
use — there's no per-chat setting to toggle:

- **Paste a link with no command** → **leeched** (sent back as a Telegram
  file). This is the default for everything.
- `/l <link>` → same as pasting it, explicit leech to Telegram.
- `/m <link>` → **mirror** to Google Drive instead (requires Drive to be
  configured — see below).
- `/zm <link>` / `/zl <link>` → same as `/m`/`/l`, but zipped into one
  file first.

## Payload grammar (every download command)

- **Batch**: `/l <link1> <link2> <link3>` — one job per link, in order.
  Rename applies only when there's exactly one link (otherwise names would
  collide).
- **Rename**: `/m <link> | myname` — the final file is named `myname`; the
  extension is never touched (`myname.mp4` stays `.mp4`, an extensionless
  name keeps the file's own extension).
- **Drive folder**: `/m <link> #holiday` — uploads into the `holiday`
  folder of the selected Drive account (created on demand). Works with
  `/m`, `/zm`, `/ym` and plain-paste mirrors.

## Multi-account Drive

Define several accounts in `.env` (`GDrive_work_*`, `GDrive_2_*`, …) and
each chat picks its own in `/settings → ☁️ Drive account`. `/drive`
browsing, `/drivesearch`, `/m` uploads and `#folder` creation all follow
that selection. Instagram indexes stay local regardless.

## Instagram indexing (local, per user/type)

Archive indexes live under `data/indexes/<user>.<type>.txt` — one flat
ledger per username and type, deliberately **not** per chat (the `/igindex`
screen says "shared across chats" for that reason: two chats archiving the
same account reuse one ledger instead of fetching the same items twice).
`/igindex` (alias `/igindexes`) lists every ledger with entry counts and
deletes selectively: one type of one user, everything of one user, or wipe
all. Deleting one makes the next archive re-fetch that content.

When Drive is configured the ledgers are mirrored into `AzBotData/indexes/`
and pulled back on a fresh deploy, so a reinstall doesn't re-download
everything.

## Zip leech / mirror

- `/zipl` — reply to a file (or pass direct links) → one zip leeched here.
  Supports batch links + `| name.zip`.
- `/zipm` — same, but the archive goes to the selected Drive account
  (`#folder` works).
- `/unzipl` — reply to an archive (`.zip .rar .7z .tar …`) or pass its
  link; every inner file is sent to this chat. `/unzipm` is an alias.

## Settings & indexes live in Drive

`settings.json` and every Instagram index are mirrored into a single
**AzBotData** folder on the bot's first Drive account:

    AzBotData/
      settings.json
      indexes/<user>.<type>.txt

Flat, not per-chat — the same account archived from two chats reuses one
ledger. Local copies under `data/indexes/` remain the working set; changes
push up automatically, a fresh deploy pulls a missing ledger down the first
time it's needed, and deleting one in `/igindex` deletes it in both places.
Rename the folder with `DATA_DRIVE_FOLDER=…`.

## Commands

- `/m` / `/l` / `/zm` / `/zl` `<link>` — see above. Also works as a
  **reply**: reply to a file already in the chat (or to a message
  containing a link) with `/m`/`/l` and no argument, and it'll fetch that
  file/link instead of needing a URL typed inline.
- `/settings` — per-chat toggle for auto-clean: when on, a successful job
  deletes your triggering message and the bot's own status message,
  leaving just the delivered file (or, for Drive-only jobs, the status
  message with the Drive link — that's kept since it'd otherwise be the
  only trace the job ran). Applies to the whole chat/group/channel; a
  forum group's topics share one setting, not one each.
- `/ig <profile_url>` — multi-select picker: tap Posts / Reels / Stories /
  Highlights / Tagged to toggle each on or off (Stories is on by default),
  then ▶️ Start to archive everything selected in one go (leeches to
  Telegram; use `/m <profile_url>` for the same picker mirrored to Drive
  instead). Re-running it only pulls what's new since last time — the
  per-type ledgers under `data/indexes/` are the skip list (see
  [Instagram indexing](#instagram-indexing-local-per-usertype)).
- `/igindex` (alias `/igindexes`) — list the Instagram archive ledgers with
  entry counts and delete one type of one user, everything of one user, or
  all of them. Deleting a ledger makes the next `/ig` run re-fetch that
  content; nothing else is touched.
- `/torrent <magnet or .torrent url>` — force aria2c (leeches to Telegram;
  use `/m` with a magnet link to mirror one to Drive instead).
- `/zm` / `/zl` `<link>` — zipped variants: `/zm` puts **one zip** in Drive,
  `/zl` leeches **one zip** to Telegram. Same payload grammar as `/m`/`/l`
  (batch links, `| name.zip`, `#folder`).
- `/gallery <url>` — force gallery-dl for a general art/photo platform
  (leeches to Telegram — see exclusions below).
- `/clone <url>` — mirror a website via `wget` (leeches to Telegram).
- `/drive` — full folder-navigating Drive browser: tap into subfolders,
  breadcrumb + "⬆️ Back", file sizes shown right in the list, "📁 New
  folder", jump-to-any-page pagination (not just forward), and tapping a
  file gives 🔗 Open / ✏️ Rename / 🗑 Delete. A folder can be deleted too
  (with confirmation) — except the configured root, which is never
  offered for deletion. Files sent to Drive via `/m`/`/zm` also get
  Rename/Delete buttons right on the completion message. `/drivesearch
  <query>` — search by name across the whole Drive.
- `/cookie` — tap a profile to switch, or the ⚙️ next to it to rename or
  delete that profile. Reply to a Netscape-format `cookies.txt` with
  `/cookie <name>` to add a new one (needed for private Instagram
  accounts, stories, highlights, age-gated videos).
- `/cancel <task_id>` / 🛑 button — stop a running job. The button now
  stays visible for the entire job (download **and** upload, to Telegram
  or Drive) instead of disappearing after the first progress update.
- `/cancelall` — admin: stop every running job.
- `/stats` — disk usage / active jobs.
- `/clean` — admin: purge stale temp files immediately.
- `/sh` — admin: **live persistent shell terminal**. Opens one real bash
  session in a PTY that stays alive between messages — `cd`, exports and
  environment persist across commands, and because it's a genuine PTY you
  get bash's full interactive line editing: ↑/↓ history recall, ←/→ cursor
  movement, TAB completion. Any non-command text you send goes to the
  shell; output streams into a single message that keeps updating. On-screen
  ⛔ Ctrl+C button (plus `/shc`) interrupts long-running commands without
  killing the session; `/shexit` (or the ⏹ button) ends it; idle sessions
  auto-close after 30 minutes. `/sh <command>` still works as a one-shot
  runner for quick things. As with any remote shell this is exactly as
  powerful as SSH access to the box, gated only by Telegram user ID — only
  set `ADMIN_ID` to an account you trust, and don't expose the bot token
  or `.env` to anyone else.
- `/help` — the sectioned help screen. Buttons: ⬇️ Downloads · 🗃 Zip & Unpack
  · 📷 Instagram · 🍪 Cookies · ℹ️ Tips & Features · 🔧 Admin, plus ⚙️ Open
  Settings and ☁️ Open Drive. `/directdownload` is described inside the
  Downloads section rather than getting its own button. Works in groups and
  forum topics; `/start` opens the same screen but is private-chat-only.
- `/allow <id>` / `/ban <id>` — admin: access control, if `ADMIN_ID` is set.
- `/driveaccounts` / `/drivelist` / `/addaccount` / `/setname` — **admin**:
  add and manage extra Drive accounts from Telegram. `/drivelogin <name>` walks through
  Google's OAuth consent screen and takes the resulting `?code=…` URL back;
  `/tokenup <name>` (alias `/tokenpaste`) is the fallback for clients that
  can't copy that URL — paste a refresh token made with `gen_token.py`
  instead. Both need `GDrive_<name>_CLIENT_ID`/`_CLIENT_SECRET` in `.env` (or
  the shared `GCP_CLIENT_ID`/`GCP_CLIENT_SECRET`), since client credentials
  can't be created by a bot.
- `/rclone` — admin: configure an rclone remote for extra clouds (Mega,
  OneDrive, Dropbox, S3, WebDAV…). Only present when `INSTALL_RCLONE=1`;
  otherwise the binary isn't installed and the command says so.
- `/px` — reply to a file and the bot uploads it to **pixeldrain** and
  replies with the shareable page plus a `/api/file/<id>?download` direct
  link. Needs a free key from
  <https://pixeldrain.com/user/api_keys>. Give `PIXELDRAIN_API_KEYS`
  several comma-separated keys and a full account rotates to the next one;
  the last working key is tried first, so the normal case is one attempt.
  A revoked key is dropped for good. With `PIXELDRAIN_FALLBACK=1` (default)
  and no key able to take the file, it falls back to keyless **x0.at**
  (1 GiB, 3–100 day links) and says which host served it.
- `/ua <direct url>` — **upload.am fetches the file on its own servers**, so
  nothing at all is downloaded to this box. That is the point on a small
  VPS: a 4 GB file costs the bot zero local bytes. Free plan is 25 GB
  storage, 5 GB per file, files kept 30 days after the last download. The
  fetch is asynchronous and upload.am has no progress endpoint, so the bot
  snapshots your file list, queues the fetch, then polls until the new file
  appears (`UA_POLL_MIN`, default 20 minutes), then posts the link. Needs
  `UA_TOKENS` (upload.am → Account → API); multiple accounts rotate the same
  way as pixeldrain. Direct links only — the URL must start the download
  itself, not open a page, so YouTube/Instagram pages still go through
  yt-dlp to local disk.
- `/ualist` — newest files in the upload.am account root, with links.
- `/directdownload` (also `/gdmlinks`) — connect an Instagram account to this
  chat with a one-time code, then DM Instagram links to the bot's Instagram
  account and the files arrive here. Shows what's supported and what isn't
  (`❌ Live streams`), how to connect, and how many accounts are linked.
  Buttons: 🔗 Connect · 🔌 Disconnect all · 🔄 Refresh. The screen also shows
  the token state (`✅ valid — 45 days left` / `⚠️ expiring` / `❌ expired`),
  since a lapsed token stops DMs silently. Requires the `IG_*` env vars and a
  public HTTPS webhook; without them the command explains exactly which
  variable is missing instead of failing silently.
- `/igtoken <token>` — **admin, private chat only**: validates a pasted token
  against Meta, deletes the Telegram message carrying it, then stores it in
  `data/ig_token.json` (mode 0600). `/igtoken refresh` renews now,
  `/igtoken status` prints the remaining life. See
  [Instagram DM downloads](#instagram-dm-downloads).

**Aliases.** One line per family, but the dispatcher accepts every spelling:

| family | accepted commands |
|---|---|
| mirror / leech | `/m` `/l` `/zm` `/zl` `/ym` `/yl` `/yt` `/ytm` `/ytl` `/ytdl` `/ytdlleech` |
| Instagram archive | `/ig` `/igm` (`/m <profile>` opens the same picker) `/igl` |
| gallery-dl | `/gallery` `/g` `/galleryl` `/gm` `/gallerym` `/galleryz` `/galleryzm` |
| website mirror | `/clone` `/clonem` |
| zip / unzip | `/zipl` `/zipm` `/unzip` `/unzipl` `/unzipm` `/unzipmulti` |
| Instagram DMs | `/directdownload` `/ddownload` `/gdmlinks`; token: `/igtoken` |
| Drive accounts | `/addaccount` `/drivelogin`, `/tokenup` `/tokenpaste` |
| shell | `/sh` `/shq` `/shc` `/shexit` `/cancelall` `/ca` |

Google Drive upload is optional — leave `GCP_CLIENT_ID` etc. blank and
`/m`, `/zm`, `/drive`, and `/drivesearch` will just tell you it's not
configured instead of failing silently.

## Public links & cloud fetch

Three destinations, picked by command:

- `/m`, `/zm` → **Google Drive** (needs GCP creds)
- `/l`, plain paste → **Telegram**
- `/px`, `/ua` → **a public URL** — the two things a Telegram upload or a
  Drive upload can't give you: something another script or bot can fetch.

Both `/px` and `/ua` accept a list of credentials (`PIXELDRAIN_API_KEYS`,
`UA_TOKENS`; comma or newline separated) and rotate automatically. The
shared `bot_pkg/keyring.py` keeps per-credential state in
`data/<host>_keys.json` — only a SHA-256 fingerprint, never the key itself —
and distinguishes three outcomes per error code:

- **rotate** — another account may well succeed (quota, plan limit)
- **dead** — this credential is permanently unusable (revoked, banned)
- **neither** — a per-IP limit, an oversized file, or a server-side fault;
  the bot stops and reports the real reason instead of burning through keys
  that cannot fix it

That distinction is why the error messages name the actual code
(`ip_rate_limit_reached`, `user_out_of_space`, `unsafe_url`, …) rather than
saying "upload failed": a per-IP limit is not something a second account
solves, and pretending otherwise just wastes time and keys.

Host limits, for reference:

| host | free storage | per file | link lifetime |
|---|---|---|---|
| pixeldrain | 10 GB | 10 GB | ~60 days idle |
| upload.am | 25 GB | 5 GB | 30 days after last download |
| x0.at (fallback) | — | 1 GiB | 3–100 days, picked by size |

pixeldrain throttles **downloads** to 6 GB/day per IP; that is a download
limit, not an upload one, and rotation does not change it.

## Instagram DM downloads

DM a link to the bot's Instagram account and the file arrives in Telegram.

### Setup

Two Meta setups work; they are not interchangeable, so pick one and keep the
token, host and account ID from the same one.

**A — Instagram Login (start here).** No Facebook Page needed.
1. Instagram app → Settings → Account type → switch to **Professional**
   (Business or Creator).
2. <https://developers.facebook.com/apps> → Create app → add the
   **Instagram** product → *API setup with Instagram Login*.
3. Add your Instagram account as an **Instagram tester** and accept the invite
   in the Instagram app (App Dashboard → Roles → Instagram testers). Without
   this step no token works, not even your own.
4. Generate an **Instagram User access token** with `instagram_business_basic`
   and `instagram_business_manage_messages`.
5. Note the numeric **Instagram account ID** (178414…) — not the @handle.
6. Put the values in `.env`:
   `IG_API_HOST=https://graph.instagram.com`, `IG_ACCOUNT_ID=<id>`, plus
   `IG_APP_SECRET`, `IG_VERIFY_TOKEN` and `IG_WEBHOOK_PORT=8080`. The token goes
   in `IG_PAGE_TOKEN` purely because that's the variable name — on setup A it
   holds an Instagram **User** token, not a Page token. Or leave it blank and
   paste it at runtime with `/igtoken <token>`, which is the friendlier path
   since it doesn't need a restart.

**B — Facebook Login.** Required only if you already run a Page.
`IG_API_HOST=https://graph.facebook.com`, token is a **Page** access token, and
the Instagram Professional account must be linked to that Facebook Page in the
Page's settings.

**Webhook (both setups).** Meta needs a public HTTPS URL with a real
certificate — self-signed is refused, and the bot's listener is plain HTTP, so
put a proxy in front:

```
# Caddy (obtains and renews the certificate itself)
your.domain.com {
    reverse_proxy 127.0.0.1:8080
}
```

Then App Dashboard → **Webhooks** → Instagram → Callback URL
`https://your.domain.com/ig/webhook`, Verify Token = your `IG_VERIFY_TOKEN`,
and subscribe to the **messages** and **messaging_postbacks** fields. The
dashboard's "Verify and save" hits the bot's GET handler immediately, so the
bot must already be running with the right `IG_VERIFY_TOKEN` when you click it.

**App Review.** For accounts that are not admins/testers of your app, submit
`instagram_business_manage_messages` for review. Your own accounts work
without it.

**Tokens expire — and renew themselves.** An Instagram Login token is valid
for 60 days and can be extended indefinitely with Meta's
`/refresh_access_token` endpoint. The bot runs a watchdog that refreshes when
fewer than 10 days remain, writes the new token to `data/ig_token.json` (mode
0600 — never `.env`, which would need editing and a restart), and keeps going.
A running bot therefore never lapses.

When a token genuinely dies (bot off for weeks, password change, permission
revoked) DMs stop arriving silently, so:

- `/directdownload` shows the token state to everyone who opens it —
  `✅ valid — 45 days left`, `⚠️ expiring soon`, `❌ expired`. A silent
  expiry would otherwise read as "the bot broke".
- `/igtoken <token>` (admin, private chat only) stores a pasted token. It is
  verified against Meta **before** being saved, the Telegram message carrying
  it is deleted immediately, and a wrong token is rejected with nothing
  written.
- `/igtoken refresh` forces a renewal now; `/igtoken status` shows the clock.
- Facebook Login Page tokens have no refresh endpoint, so the bot says exactly
  that instead of pretending to renew them — regenerate those in the App
  Dashboard and paste with `/igtoken`.

The token is never logged. Failures record Meta's own error message only.

**Last check.** `/directdownload` in Telegram should show the feature list and
"not configured" should be gone. Tap 🔗 Connect, DM the code to your Instagram
account, and the DM thread answers "✅ Linked".

**Why it works this way.** Instagram DMs are readable only through Meta's
official Instagram Messaging API. That requires: an Instagram **Professional**
account (Business or Creator), a Meta app with the messaging permission
(`instagram_business_manage_messages` on setup A,
`instagram_manage_messages` on setup B — App Review for anyone who is not an
app admin or tester), and a public HTTPS webhook. A Facebook Page is required
only on setup B. There is no password-based path, and this bot never asks for
one.

**Linking.** `/directdownload` mints a six-character code. Send it as a DM to
the bot's Instagram account; the bot pairs that Instagram-scoped sender ID
with your Telegram chat and deletes the code. Codes are single-use and expire
after 30 minutes. What is stored on disk (`data/ig_dm.json`, mode 0600) is the
IGSID → chat mapping only — no credentials, no OAuth tokens, no passwords.

**What is supported**

| content | status | note |
|---|---|---|
| Post (single image or video) | ✅ | |
| Carousel | ✅ | every slide |
| Reel | ✅ | |
| Story | ✅ | needs a cookie profile (`/cookie`) |
| Highlight | ✅ | same cookie requirement |
| Live stream | ❌ | no Meta download API; recording someone else's live is a legal problem as well |

Stories and highlights are served by Instagram only to logged-in sessions, so
they use the same cookie profile `/ig` already uses. Disappearing/view-once
media is not supported by Meta's media webhooks at all. A DM containing only a
gif or sticker triggers no webhook — that is a platform limit, not a bug here.

**Operational notes.** The webhook is a plain `ThreadingHTTPServer` on
`IG_WEBHOOK_PORT`; TLS and the public hostname belong to a reverse proxy in
front of it. Every POST is verified against `IG_APP_SECRET` — a request with a
missing or wrong signature is rejected before any work happens, because
otherwise anyone who guessed the URL could make the bot fetch arbitrary links.
Delivery is asynchronous: the response goes out first, then the download is
queued through the same job queue as every other command, so a burst of DMs
cannot exceed the worker count. `IG_WEBHOOK_PORT=0` disables the whole feature
and opens no socket.

## Environment variables

Everything the code reads, grouped, with the default it falls back to. Blank or
missing means the default in every case — the values are read with `or default`,
so an empty string behaves like an absent one.

**Required**

| var | meaning |
|---|---|
| `API_ID`, `API_HASH` | from <https://my.telegram.org/apps>; Pyrogram needs them even for a bot |
| `BOT_TOKEN` | from @BotFather. `TELEGRAM_TOKEN` is accepted as an alias |
| `ADMIN_ID` | Telegram user id with admin rights; unset = no access control at all |

**Downloads and queue**

| var | default | meaning |
|---|---|---|
| `QUEUE_WORKERS` | 1 | concurrent download+upload pipelines; raise if you serve many chats |
| `UPLOAD_WORKERS` | 1 | concurrent uploads inside one job (memory-bound) |
| `HANDLER_WORKERS` | 6 | thread pool for handlers, so a slow probe never blocks taps |
| `STALL_TIMEOUT_S` | 600 | kill a subprocess job after this long with no output |
| `YT_DLP_OPTIONS` | — | JSON object merged over the bot's yt-dlp defaults |

**Google Drive (all optional)**

| var | meaning |
|---|---|
| `GCP_CLIENT_ID`, `GCP_CLIENT_SECRET`, `GCP_REFRESH_TOKEN` | first account, named `main` |
| `GDrive_<name>_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN` | extra accounts, any suffix |
| `DRIVE_FOLDER_ID` | root folder for mirrors |
| `DATA_DRIVE_FOLDER` | settings/index mirror folder name (default `AzBotData`) |
| `DRIVE_UPLOAD_CONCURRENCY` | 1 = serial, the safe profile |
| `GOOGLE_REDIRECT_URI` | OAuth redirect for `/drivelogin` (default `http://localhost:1/`) |
| `INSTALL_RCLONE` | 1 = install rclone for `/rclone` remotes |

**Public links**

| var | default | meaning |
|---|---|---|
| `PIXELDRAIN_API_KEYS` | — | one or more pixeldrain keys, comma/newline separated |
| `PIXELDRAIN_COOLDOWN_H` | 6 | hours a quota-full key is skipped |
| `PIXELDRAIN_FALLBACK` | 1 | fall back to keyless x0.at when no key can take the file |
| `UA_TOKENS` | — | upload.am tokens, same list syntax |
| `UA_FOLDER_UUID` | root | destination folder for `/ua` fetches |
| `UA_POLL_MIN` | 20 | minutes `/ua` waits for the fetch to appear |
| `UA_COOLDOWN_H` | 6 | hours a rate-limited token is skipped |

**Instagram DMs**

| var | default | meaning |
|---|---|---|
| `IG_APP_SECRET` | — | verifies every webhook payload's HMAC signature |
| `IG_PAGE_TOKEN` | — | starting token; a stored/refreshed one in `data/ig_token.json` wins |
| `IG_ACCOUNT_ID` | — | numeric Professional account id (178414…) |
| `IG_VERIFY_TOKEN` | — | must match the App Dashboard's verify token |
| `IG_API_HOST` | `https://graph.instagram.com` | or `https://graph.facebook.com` for Facebook Login |
| `IG_API_VERSION` | `v23.0` | bump when Meta retires a version |
| `IG_WEBHOOK_PORT` | 0 | 0 = feature off, no socket opened |
| `IG_WEBHOOK_PATH` | `/ig/webhook` | must match the callback URL path |

**Runtime and diagnostics**

| var | meaning |
|---|---|
| `LOG_LEVEL` | `INFO` by default |
| `DISABLE_TGCRYPTO=1` | skip the TgCrypto self-test, always use pure-Python crypto |
| `TGCRYPTO_SELFTTEST=0` | skip the self-test, always trust TgCrypto |

## Self-checks

Three assert-based scripts, no test framework, no network except where noted.
Run them with the interpreter that has the bot's dependencies installed:

```
python3 selftest_uploads.py      # /px + /ua key rotation, 12 assertions, offline
python3 selftest_igdm.py         # IG DM classification, webhook signature, codes
python3 selftest_igtoken.py      # token precedence, expiry states, refresh, perms
```

`selftest_uploads.py` covers both upload hosts, so there is no separate
pixeldrain script; `selftest_pixeldrain.py` and `selftest_uploadam.py` were
merged into it. All three exit 0 and print a one-line `OK — …` summary.

## Reliability

- **Cancel works at every stage**, not just download — including mid-upload
  to Telegram or Drive — and the 🛑 button no longer drops off the status
  message partway through a job (both were bugs in an earlier version).
- **Stalled jobs stop themselves.** `/torrent`, `/gallery`, and `/clone`
  (and the `/ig` archiver) run a watchdog: if a process produces zero
  output for `STALL_TIMEOUT_S` (default 240s — e.g. a torrent with no
  peers, or a site silently blocking gallery-dl), it's terminated and you
  get a clear "⏱️ Stopped — no progress for N minutes" message instead of
  the job hanging forever with a stale status line.
- **Unexpected errors are never silent.** Every command and button is
  wrapped so that if something throws an exception the bot didn't
  anticipate, you get "❌ Something went wrong…" in chat with the actual
  error, instead of the bot just not responding. Harmless "message not
  modified" edits (e.g. tapping a button that leads to content already on
  screen) are swallowed quietly rather than reported as errors.
- **Duplicate updates are ignored, and duplicate processes refuse to start.**
  Two things guard against doubled replies: an in-memory check drops any
  update that somehow gets delivered twice, and — the more important one —
  the bot now takes an OS-level lock on `data/azleechbot.lock` at startup.
  If a second instance tries to start while one's already running (a
  redeploy that didn't cleanly stop the old container, e.g.), it refuses
  to start and logs exactly why, instead of both silently answering every
  command twice. This works across containers too, as long as they mount
  the same `data/` volume.
- **`/m`/`/l`/`/zm`/`/zl` work as replies.** Reply to a file already in the
  chat, or to a message containing a link, with one of these and no
  argument, and it's used instead of requiring a URL typed inline.

## Logging

Everything of note — job start/finish, uploads, Drive calls, cancellations,
admin actions, auth denials, errors — goes through Python's `logging`,
configured in `bot_pkg/log.py`:

- **Console**: printed live, level controlled by `LOG_LEVEL` in `.env`
  (default `INFO`; set `DEBUG` for more detail, e.g. per-worker job pickup).
- **File**: `data/logs/bot.log`, rotated at 10MB with 3 backups kept, so
  it survives a restart and you can `tail -f data/logs/bot.log` on a
  headless host.

Pyrogram's own connection/session logging is kept at `WARNING` so it
doesn't drown out the bot's own logs — bump `logging.getLogger("pyrogram")`
in `bot_pkg/log.py` if you need to debug Pyrogram itself.

## About the WZML-X comparison

A few things adopted from [WZML-X](https://github.com/SilentDemonSD/WZML-X)
(a much larger fork of `mirror-leech-telegram-bot`) that fit cleanly into
this bot's architecture:

- **HTML parse mode everywhere** — WZML-X's approach. Markdown's delimiter
  auto-balancing was producing entity tables that overrun the message text
  (Telegram error `ENTITY_BOUNDS_INVALID`, "❌ Something went wrong") when
  filenames/log lines/URLs contained backticks or brackets. HTML entities
  have deterministic bounds, so the whole error class is gone.
- **Quality picker with sizes on every media link** — `/m`, `/l` and the
  `/yt`-family all probe the video first and offer real resolutions with
  approximate file sizes (`🎬 1080p (486 MB)`), like WZML-X's yt-dlp flow.
  Picking from formats that actually exist also sidesteps "Requested format
  not available" errors.
- **Media metadata pass before upload** — downloads are remuxed
  (`ffmpeg -c copy`, no recoding) and duration/dimensions are read back
  via ffprobe and passed to Telegram. This fixes videos showing as
  "0 seconds" in the player after a DASH merge drops the header tags.
- Command aliases matching its names: `/y`/`/ytdl` (mirror via yt-dlp),
  `/yl`/`/ytdlleech` (leech via yt-dlp) — on top of this bot's own `/m`/`/l`,
  which already auto-detect yt-dlp links so these need no separate code path.
- `YT_DLP_OPTIONS` env var — raw JSON merged into yt-dlp's options, same
  concept as WZML-X's config key of the same name, for tweaking yt-dlp
  behavior without touching code.
- `/settings` → "Always send as document" — skips Telegram's video
  re-encoding/thumbnail generation, same idea as its `AS_DOCUMENT` setting.
- **yt-dlp is NOT auto-updated** (removed by user request): restart the
  bot (after `pip install -U yt-dlp` or an image rebuild) to pick up a
  newer version. YouTube extraction errors like "The page needs to be
  reloaded" are almost always yt-dlp lagging YouTube's changes — update
  and restart when they appear.
- **`extractor_args` player_client fallback** (`tv`, `android`, `web`) —
  YouTube's default web client increasingly requires a PO token that
  yt-dlp can't always obtain; these clients don't, and this is the
  standard community/WZML-X workaround for that specific failure mode.

What I didn't do: make this bot "identical" to it. WZML-X is a much larger,
differently-architected project — MongoDB-backed, with qBittorrent,
JDownloader, SABnzbd, Mega, and rclone as separate service integrations
plus its own web UI — and it's licensed **AGPL-3.0**. Copying its code
directly would mean this bot inherits that license, which has a real
consequence worth knowing: AGPL's network-use clause means if you run a
modified version as a service, you're required to make the complete
source available to anyone who uses it. That's a legal commitment, not
just a style choice, and not something to take on silently. If there's a
specific feature from it you want (torrent file-selection UI before
download, Mega.nz support, rclone as a destination, qBittorrent instead
of aria2c), tell me which one and I'll build that piece specifically
rather than guessing at the whole thing.

## What's intentionally not here

The old curated "gallery site" allowlist/blocklist is gone entirely —
`/gallery` now runs gallery-dl against whatever URL you give it, and
everything `gallery-dl`/`yt-dlp` support works normally.

## What changed from the original `pyTelegramBotAPI` version

- **Library**: `pyTelegramBotAPI` → Pyrogram + TgCrypto, which is what
  actually gets you past the 50MB limit — Pyrogram talks MTProto directly
  instead of going through the HTTP Bot API.
- **Auto-leech**: any message containing a supported URL (or a magnet
  link) is now picked up automatically with no command at all; the
  original always required `/l`, `/m`, etc.
- **Destination**: purely a function of which command you use (`/m` vs
  `/l`, or no command = leech) — no per-chat "destination" setting to
  toggle, matching how the original's `/m`/`/l` pair worked.
- **Instagram tracker**: reimplemented on top of `gallery-dl --download-
  archive` instead of a hand-rolled index — same "only fetch what's new"
  behavior, less custom code. Unlike the original's "no data kept on the
  server" design, the ledgers live in `data/indexes/` locally and are
  mirrored up to `AzBotData/indexes/`, so a run works offline and a fresh
  deploy still gets its history back.
- Torrent (`aria2c`), website mirroring (`wget`), and the admin shell are
  carried over essentially as-is.

## Performance

- **Downloads use aria2c (16 connections)** instead of a single HTTP
  stream, both for direct file links and for plain-HTTP(S) yt-dlp
  downloads — real multi-connection speed instead of one TCP stream
  trickling in. HLS/DASH fragment downloads stay on yt-dlp's own
  downloader, since aria2c doesn't handle segment decryption/merging.
- **Telegram uploads/downloads use 8 concurrent file-part transmissions**
  (Pyrogram defaults to 1), which is the single biggest lever for
  large-file transfer speed over MTProto.
- **Drive uploads use 50MB chunks** instead of 10MB, cutting per-chunk
  HTTP round-trip overhead on large files.

## Notes

- Telegram's ~2GB per-file cap for bots is a platform limit, not a
  library one — it applies the same whether you use Pyrogram, Telethon,
  or a local Bot API server. Files bigger than that get split into parts
  automatically.
- yt-dlp needs occasional updates as sites change their pages; if a
  previously-working site starts failing, `pip install -U yt-dlp` first.
