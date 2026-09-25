# AzBot fixes — drive-folder albums + split-zip (WZML-X-inspired)

Date: 2026-09-11. Source: your uploaded `azdrivedlbot-pyro2GB.zip`.
Only 2 files changed, 4 small hunks. Everything else is byte-identical
(`diff -r` shows just these).

## Fix 1 — images not sent as albums + slow (bot_pkg/dispatcher.py)

ROOT CAUSE (proven with a repro, not guessed):
`_upload_one()` buffers album entries as 5-tuples
`(fpath, cap, fsize, kind, vmeta)`, but THREE unpack sites in
`_send_album()` unpacked only 4. Every one raised
`ValueError: too many values to unpack`:
- line 399 single-leftover path: `p, cap, fs, kind = batch[0]`
- line 438 success-path `on_sent` loop
- line 464 success-path delete loop

Effect: the `send_media_group` call on line 427 SUCCEEDED, then the very
next loop crashed into the `except` fallback — which re-sent the same
photos as SINGLES. So every album went out twice (once as a group, once
as singles, or singles only if the group exception path won), each single
a separate FloodWait candidate. That is exactly your symptom: slow +
"images without being sent as albums".

This matches WZML-X's design (`MEDIA_GROUP`: one SendMultiMedia per 10
files instead of ten individual sends — the difference between minutes
and an hour of FloodWaits). Your bot already had that design; it was
just broken by the arity mismatch.

FIX: unpack 5 everywhere (`_meta`/`_meta0` where unused). Verified:
AST check confirms all `for ... in batch` loops unpack 5-tuples; full
repo `py_compile` passes.

NOTE — singles are still CORRECT in these cases (not a bug):
- `/settings` → "Send as document" ON forces singles.
- `.webp` (sticker risk), `.gif`/`.bmp`/`.heic` (not in PHOTO_EXT) go single.
- small videos without ffprobe duration fall back to single (needs
  `MEDIA_EMPTY` attributes — the code already probes; on a weak box
  keep ffmpeg+ffprobe installed).
- one trailing leftover (<2 items) goes via normal uploader —
  `send_media_group` needs 2–10 items.

SPEED on a low-resource box (unchanged behavior, just now working):
albums cut Telegram sends ~10x. Drive downloads stay sequential
(single-thread walk + process-wide Drive `_api_lock`) — that is
intentional (it fixed the "free(): corrupted unsorted chunks" OOM/crash),
so a 500-file folder is still minutes, but no longer 10x sends on top.
`UPLOAD_WORKERS` (env, default 2) tunes parallel uploads; raise only if
RAM allows (each worker = MTProto buffers + ffmpeg/ffprobe).

## Fix 2 — split .zip "no end-of-archive index" (bot_pkg/utils.py)

Your error text comes from `_extract_multipart()` in handlers_zip.py
(it concatenates `file.zip.part001..003`, checks the joined bytes for
the zip central-directory index, tries `zip -FF`, else raises exactly
that message). It means ONE of these — in order of likelihood:

1. INCOMPLETE SET. Caption says `.part001/004` but only 3 were
   replied/selected → fetch/send `.part004` onward. The bot already
   aborts early with "Set is incomplete: caption says N parts but only
   M are here" when captions carry totals — always reply with ALL parts
   or use `/unzipmulti` (send parts one by one → ✅ Done).
2. A part arrived truncated (Telegram download short). The bot verifies
   size + split-time MD5 per part and retries once; a repeat MD5
   mismatch means the copy IN THE CHAT is damaged — re-leech it.
3. SOURCE was already truncated when split (its own EOCD missing). If
   every part verifies byte-exact (MD5 lines in captions) and the error
   persists, the code says so explicitly: "the SOURCE zip was already
   truncated when the bot split it… re-fetch the original file."
4. WRONG OPERATION: a `.bin` is NOT an archive. If your 5 GB file was
   `file.bin` (firmware/disk image), `/unzip`/`/unzipm` can NEVER work —
   there is no index to find. Mirror it instead: reply to the parts
   with `/m` (Drive) — no reassembly, no extraction.

LOW-RESOURCE REALITY for a 5 GB reassembly: join needs ~5 GB free,
`zip -FF` repair needs ANOTHER ~5 GB + ≥80 MB free RAM + the `zip`
binary, and 7z extraction needs room for the OUTPUT. On a 250 MB-class
box that fails by design. Prefer: mirror parts as-is, or reassemble on
a bigger machine (`cat file.zip.part* > file.zip`, check with
`7z t file.zip`, then upload).

FIX in this patch: `zip_tail_ok()` only looked for the classic EOCD
(`PK\x05\x06`). Healthy >4 GB zip64 archives carry `PK\x06\x06`
(zip64 EOCD) / `PK\x06\x07` (locator); the old check false-rejected
them as "truncated" both at split time (uploader refuses to split) and
at reassembly. Now all three signatures accept. (Genuinely truncated
files still fail — as they should.)

## Fix 3 — one bad file no longer flattens its whole album (dispatcher.py)

Follow-up symptom: albums arrive, but singles keep appearing BETWEEN
albums (album → singles → album → …).

Two possible causes — check which is yours:

A. OLD BUILD STILL LIVE. Before Fix 1, every group of 10 was sent as
   an album AND THEN re-sent as 10 singles (the crash-into-fallback).
   Signature: the singles DUPLICATE photos already in the albums.
   Fix: deploy `dispatcher.py` from this package and restart.
B. MIXED FORMATS / POISON FILE (with Fix 1 live). Singles that are
   DIFFERENT photos from the album ones are correct-or-explained:
   - `.HEIC` (iPhones!), `.GIF`, `.BMP`, `.TIFF` are not in PHOTO_EXT
     (Telegram photo groups only take JPEG/PNG) → single documents.
   - "Send as document" ON in /settings → everything single.
   - exactly ONE leftover photo at the end (Telegram needs ≥2 for a
     group) → single. Unavoidable, same in WZML-X.
   - one corrupt/oversized image POISONED its group: Telegram rejects
     the whole group of 10, and the old code then sent all 10 as
     singles. This patch fixes exactly that: a failed group is SPLIT
     IN HALF and each half retried as its own group (recursively down
     to pairs; only a pair that won't group goes single). One bad
     file now costs at most its immediate neighbours their album slot,
     never the other nine.
   Verified with a simulated poisoned batch (10 photos, img07 kills
   any group containing it): result = groups [01–05] + [08–10],
   singles [06, 07], zero leftovers on disk, every photo exactly once.

## Improvements 1/2/3/5 (second batch, 2026-09-12)

1. BUILD STAMP — new `bot_pkg/build_info.py` (git hash + newest .py
   mtime). Logged at startup (`core.start`) and shown in `/stats`
   (`handlers_admin`). After a redeploy, check the stamp: if it didn't
   move, the old process is still running — restart, don't debug ghosts.
2. NO MORE PER-PHOTO FFPROBE — `uploader._send_one` decided
   photo-vs-document via an ffprobe subprocess per image (hundreds of
   spawns per folder on a small box). Now `_image_dimensions_fast`
   reads dimensions straight from file headers (JPEG SOF / PNG IHDR /
   GIF / BMP, pure Python, ~0.03ms/file) and the original rule is
   restored: min-dimension ≥480px AND >50KB → photo, else document.
   Unparseable files read as (0,0) → document (safe direction); the
   existing PHOTO_INVALID_DIMENSIONS fallback still catches odd files.
   Verified 9/9 incl. progressive-JPEG, truncated and garbage inputs.
3. ALBUMS TOGGLE — `state.get/set_media_group` (default ON, persisted
   per chat like as_document), wired into the dispatcher album gate and
   into /settings as "🖼 Albums: ON/OFF" with its own callback.
   WZML-X MEDIA_GROUP parity.
5. UNZIP GUARDS — in `run_unzip_local_job`: (a) bare `.partNNN` slices
   whose base is NOT an archive (`.bin .img .mkv…`) are refused
   instantly with "nothing to extract — mirror with /m" instead of
   downloading gigabytes into a cryptic 7z failure; (b) disk pre-check
   before downloading (parts + headroom) with real numbers; (c) second
   disk check in `_extract_multipart` before the join (join + repair +
   output need room). Verified: .zip/.tar.gz parts pass, .bin/.mkv/.img
   blocked; full-repo py_compile clean.

## Gallery rule (2026-09-12, per user: NO conversions)

A file that meets Telegram's gallery criteria (photo, or small video
with real duration/dims) rides the album; anything else goes alone as
a single send. User-requested exceptions (targeted, best-effort, with
fallback to the original — never worse than before):

- static webp→jpg (ORIGINAL sticker defense, kept throughout).
- animated webp→gif (2026-09-12): keeps the animation in a universally
  viewable file, sent via send_animation as a native looping GIF
  (autoplay, not a sticker card). Still goes ALONE — Telegram groups
  accept photo+video only, so no conversion can put it in an album
  (mp4 would be the only album vehicle, not requested). GIFs now get
  ffprobe metadata (duration/dims) via MEDIA_PROBE_EXTS.
- Verified: static webp→jpg (640×480 intact); multi-frame gif
  roundtrip (10/10 frames, decodes clean); ANIM detection
  true/false correctly. (This sandbox's ffmpeg ANIM muxer can't emit a
  valid animated webp test input, so the animated decode leg is
  covered by the proven webp decoder + failure-fallback, not an
  end-to-end run.)

## Full-audit batch (2026-09-12): 22 verified bugs fixed

Three parallel reviewers + my own line-by-line verification (1 claimed
"bug" rejected: the alleged double album flush can't happen —
flush_album guards empty batches). 29/29 checks green, full-repo
py_compile clean, album sim still green.

CRITICAL: ZipSlip-proof extraction (containment + entry/size caps);
aria2 `float("10MiB")` crash→hang fixed with a unit parser + per-line
resilience; Drive delete/rename/get/create/list/search now take the
per-chat account (wrong-account data loss gone).

MAJOR: stale Drive services actually evicted (keyed `(name, ident)`);
split-video total failure raises and keeps the source; hostile
Content-Disposition names confined to basename; webp/gif conversion
never clobbers same-stem siblings (`.conv` beside); downloads lock
per-chunk instead of stalling all Drive traffic; datastore under lock
+ atomic `.part`+replace writes; Google `4/…` auth codes parsed via
query string; dead-account fallback picks a live account; Drive zip
uploads honor `#folder`/rename; batch duplicates skip instead of
rejecting all; unpack telegram branch collects `failed[]` like Drive;
7z gets `stdin=DEVNULL` (password fail-fast); part downloads catch
FloodWait/resets with named failures; mirror part-links queue per
filename; zip job guards `None` sources + combined summary; `.z01`
sets get sibling scans + dominant-base filtering for every scheme;
dead duplicate volume code deleted.

MINOR: purge loop can never die; album counters un-count failures;
photo fallback surfaces the real error; `-f 'single-quoted'`
selectors work; URL `#fragments` no longer become folders; streaming
socket closed; shell warns once instead of eating messages + stops
sessions on drop (no FD leak).

## Apply

Copy the changed files over your bot source (or unzip this package):
- `bot_pkg/dispatcher.py`
- `bot_pkg/utils.py`
- `bot_pkg/build_info.py` (new)
- `bot_pkg/core.py`
- `bot_pkg/state.py`
- `bot_pkg/uploader.py`
- `bot_pkg/handlers_core.py`
- `bot_pkg/handlers_admin.py`
- `bot_pkg/handlers_zip.py`

Then:
```
python3 -m py_compile bot_pkg/dispatcher.py bot_pkg/utils.py
# restart the bot
```

## Your exact cases — what to do now

A. Drive folder with hundreds of images → update dispatcher.py, then
   leech again. Photos should arrive in groups of ≤10. If some still
   come single, check the NOTE list above (`/settings` as-document is
   the #1 culprit).
B. The 5 GB `.bin`/`.zip` 3-part set → do NOT `/unzip` a `.bin`.
   If it IS a zip: confirm caption totals (003/003 vs 003/004), confirm
   MD5 lines exist, reply with ALL parts at once (or `/unzipmulti`),
   ensure disk + `zip` + `7z` present. If MD5s all verify and it still
   fails → source truncated → re-fetch origin, the parts are unusable.
