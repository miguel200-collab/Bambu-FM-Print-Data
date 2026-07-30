# AGENTS.md — Project state for new chats

> Read this first. It captures what the project is, what's built, what's
> deployed, what's broken right now, and the plan. Keep it updated as work
> progresses so the next chat starts on the same page.

---

## 0. Where we are right now (Jul 30, 2026, ~11:45 ET)

**Goal of this session:** fix the 403 download blocker that was preventing
student submissions from being archived, and clean up the confusing
Yes/No print-result popup. (Prior Jul 29 session set up the laptop; prior
Jul 21 session completed the web side — both preserved as "Prior session"
subsections below for history.)

### What was done this session (Jul 30)

1. **Root-caused the 403 download blocker with a curl test from the laptop**
   against the stuck blob's plain `url`
   (`Miguel__00M09A390900243__BROKER-2-reachy-mini-1.gcode.3mf`):
   - With `Authorization: Bearer <BLOB_READ_WRITE_TOKEN>` → **HTTP 200**
     (full 2.97 MB file returned, `Content-Length: 2973705`).
   - Without the header → **HTTP 403 Forbidden**.
   This proved the plain `url` IS fetchable for a private store *with* the
   Bearer token — the daemon simply wasn't sending it. The signed
   `downloadUrl` is missing on the webhook path (Vercel's `onUploadCompleted`
   callback doesn't forward a usable one for this store), so the daemon was
   falling back to the plain `url` and 403ing. The full chain
   (`route.ts` → `api_server.py` `BlobWebhookPayload` → `enqueue_blob` →
   `_process_blob` → `_download_blob`) was traced and is correct; the missing
   field is Vercel-side, not in our code.

2. **Fixed `_download_blob` in `station/file_watcher.py`** — added
   `Authorization: Bearer {self._token}` to the GET, mirroring the auth
   already used by `_list_pending_blobs` and `_delete_blob`. ~3 lines. This
   makes both the signed `downloadUrl` (header harmless) and the plain `url`
   fallback work for private stores, and also rescues old blobs in the
   catch-up `list` whose signed URLs expired (>7 days). Verified
   `python -m py_compile station/file_watcher.py` clean; no linter errors.

3. **Fixed the confusing print-result popup in `labeler_gui.py`.** The old
   wording was cognitively backwards: the question was negative ("Did this
   print fail?"), so YES meant bad and NO meant good — opposite of every
   other dialog. Changed the question to the neutral "How did this print
   turn out?" and dropped the YES/NO prefix from the buttons — they now
   just read "Print Succeeded" (green) and "Print Failed" (whiteish).
   Colors, layout, and the `0`=succeeded / `1`=failed label mapping are
   unchanged, so no database format change. Verified `python -m py_compile
   labeler_gui.py` clean; no linter errors.

4. **Committed the 403 fix + popup fix** in `03069d0` (pushed to `origin/main`)
   and updated this `AGENTS.md`. The two Jul 29 bug fixes (`main.py` `log`,
   `database.py` `blob_key`) were already committed in `0b9045f` on Jul 29
   and pushed to `origin/main`.
5. **Verified the 403 fix end-to-end on the laptop** (`git pull` → restart →
   fresh upload). Both the catch-up blob and the fresh webhook upload
   downloaded with `HTTP/1.1 200 OK` — the Bearer-auth fix works. This
   surfaced a **new, smaller bug** one step later: `NameError: name
   '_Timeout' is not defined` in `printer_uploader.py` (a typo — the
   constant is `_TIMEOUT` but was referenced as `_Timeout`). It was always
   there but unreachable while the 403 blocked the pipeline earlier.
6. **Fixed the `_Timeout` typo** in `station/printer_uploader.py` (line 194:
   `_Timeout` → `_TIMEOUT`) and **made the printer push non-fatal** in
   `station/file_watcher.py` `_process_blob`: the `printer_uploader.upload_file`
   call is now wrapped in `try/except` so a push failure (or "no available
   printer") logs a warning but still archives the file to the inbox and
   deletes the blob. This aligns with the decided workflow (the human
   uploads via BFM; the laptop's push is a deprecated best-effort
   convenience). Committed in this follow-up commit. **Awaiting fresh
   end-to-end re-test on the laptop.**

### Status of prior blockers (with dates — preserved for history)

- ✅ **Bug A — `NameError: name 'log'` in `main.py` `_start_station_services`**:
  found + fixed Jul 29, committed in `0b9045f` Jul 29, pushed to `origin/main`.
- ✅ **Bug B — `IndexError: row["blob_key"]` in `database.py`
  `list_recent_uploads`**: found + fixed Jul 29, committed in `0b9045f`
  Jul 29, pushed to `origin/main`.
- ✅ **403 Forbidden downloading private blobs**: found Jul 29 (see "Prior
  session (Jul 29)" §0 below for the full diagnosis), root-caused + fixed
  Jul 30 (this session) via Bearer auth on the GET in `_download_blob`.
  Committed this session. **Awaiting end-to-end verification on the laptop.**
- ✅ **Confusing Yes/No popup wording**: fixed Jul 30 (this session). Visible
  the next time a print job ends on the laptop after a daemon restart with
  the new code.
- ✅ **`NameError: name '_Timeout' is not defined` in `printer_uploader.py`**:
  found Jul 30 during the end-to-end re-test (the 403 fix unblocked the
  pipeline enough to reach this previously-unreachable line). Root cause =
  a typo (`_Timeout` vs the defined `_TIMEOUT`). Fixed Jul 30 by correcting
  the typo AND making the printer push non-fatal in `_process_blob`.

### What's left (next chat)

1. **Re-verify the full happy path on the laptop** after the printer-push
   fix: `git pull` (to get this follow-up commit) → restart `python main.py`
   → upload a fresh `.gcode.3mf` from `https://bambu-farm-web.vercel.app`
   → confirm the laptop logs `Archived <renamed> (target <printer>) to
   C:\BambuSubmissions\<name>\<renamed>` and the blob is deleted from the
   Vercel Blob dashboard's `incoming/` folder. The 403 download step is
   already confirmed working (200 OK); this re-test confirms the file now
   actually lands in the inbox (the previous run 403'd-then-`_Timeout`'d and
   left nothing archived). The stuck Jul 29 blob may still be in Vercel —
   it should get processed and deleted on startup catch-up.
2. **Regenerate the two leaked secrets.** During diagnosis this session the
   user pasted the real `BLOB_READ_WRITE_TOKEN` and `STATION_WEBHOOK_SECRET`
   in plaintext into the chat (and a prior session had a screenshot leak).
   After the 403 fix is verified, regenerate both:
   - Webhook secret: `openssl rand -hex 32` → update `config.json`
     `web.webhook_secret` AND Vercel env var `STATION_WEBHOOK_SECRET` → redeploy.
   - Blob token: regenerate in Vercel Blob store Settings → update
     `config.json` `web.blob_token` AND Vercel env var `BLOB_READ_WRITE_TOKEN`
     → redeploy.
3. **(Optional) Make the Python daemon survive reboot** via
   `install_autostart.bat` (the Tailscale Funnel already survives reboot via
   the `tailscaled` service). Not yet done.
4. **(Optional) Update `station/TUNNEL.md`** to document the Tailscale Funnel
   path as first-class (the doc currently only covers Cloudflare Tunnel).
   The user is actually running Tailscale Funnel.

### Key live values (for the next chat — DO NOT commit these)

- **Tailscale tailnet:** `tailfc0d71.ts.net`, account
  `ramirezperazamiguel@gmail.com`, laptop device `desktop-rck343r`
  (Tailscale IP `100.66.174.32`, Windows 10 1909, Tailscale 1.58.10).
- **Funnel URL (the `station_api_url` / Vercel `STATION_API_URL`):**
  `https://desktop-rck343r.tailfc0d71.ts.net` (serving `localhost:8080`).
- **Laptop repo path:** `C:\Bambu-FM-Print-Data`.
- **Inbox:** `C:\BambuSubmissions\<sanitized student name>\`.
- **Vercel Blob store:** `bambu-submissions` (Private, IAD1),
  store ID `store_GsbGH5ITT9oNw0eV`, base URL
  `https://gsbgh5itt9onw0ev.private.blob.vercel-storage.com`.
- **Printers (4):** The Allee (192.168.1.172, 00M09A390900243), The Room
  (192.168.1.89, 0938AC5C2300029), The Forecourt (192.168.1.111,
  00M00A342800191), The Bust (192.168.1.169, 00M09A3C0401135).

### Prior session (Jul 29, 2026) — laptop setup (preserved for history)

> Status as of Jul 29. The two bugs and the 403 blocker documented below
> have since been RESOLVED — see "Status of prior blockers" above for the
> resolution dates. This subsection is kept verbatim so the full diagnosis
> and setup steps remain visible across the project's history.

**Goal of that session:** set up the dedicated Windows laptop end-to-end so
student uploads flow Vercel → laptop → inbox in real time. (Prior Jul 21
session completed the web side — see "Prior session (Jul 21)" at the end of
this section.)

#### What was done that session (laptop setup, in order)

1. **Backed up** `config.json` + `print_dataset.db` (saved from the old
   MQTT-only daemon) and stopped the old daemon.
2. **Installed Git** on the laptop, `git clone`d the repo into
   `C:\Bambu-FM-Print-Data`, restored the two backed-up files into the clone.
3. **`pip install -r requirements.txt`** did NOT install `httpx` (multiple
   Python installations on Windows; `pip` resolved to a different Python than
   `python`). Fixed by re-running as `python -m pip install -r
   requirements.txt` (the `python -m pip` form guarantees the same
   interpreter). Verified with `python -c "import httpx, fastapi, uvicorn;
   print('ok')"`.
4. **`mkdir C:\BambuSubmissions`** (the inbox root).
5. **Edited `config.json`** to add the `web` section (printers array left
   untouched from the backup): `blob_token` = the `BLOB_READ_WRITE_TOKEN`
   copied from the Vercel Blob store's Settings page (the `.env.local` tab in
   the Quickstart shows it in plaintext — NOT masked there, unlike the project
   Environment Variables page); `webhook_secret` = the user's saved
   `STATION_WEBHOOK_SECRET` (matches Vercel; not regenerated);
   `station_api_url` = the Tailscale Funnel URL (step 6); `api_host`
   `127.0.0.1`, `api_port` `8080`, `poll_interval_s` `0`, `inbox_dir`
   `C:\\BambuSubmissions`.

6. **Skipped Cloudflare Tunnel** (user can't buy a domain right now) and used
   **Tailscale Funnel** instead — free, no domain needed, stable `*.ts.net` URL.
   - Created a Tailscale account (from the Mac), installed Tailscale on the
     laptop, `tailscale up` (laptop now in the tailnet as `desktop-rck343r`,
     tailnet domain `tailfc0d71.ts.net`).
   - Enabled Funnel via the admin console: **Access controls → JSON editor →
     "Add Funnel to policy"** button (adds the `funnel` grant to the ACL).
     NOTE: there is NO Funnel toggle under Settings or DNS — it's purely
     ACL-gated in current Tailscale. The user also added a
     `{"src":["*"],"dst":["*"],"ip":["*"]}` grants rule (permit-all for
     tailnet-internal access; harmless, leave it).
   - `tailscale funnel 8080` (foreground test) → printed the public URL.
   - Made it persistent: `tailscale funnel --bg 8080` (registered with
     `tailscaled` Windows service; survives reboot; can close all terminals).
   - **Funnel URL format:** `https://desktop-rck343r.tailfc0d71.ts.net`
     (device-name.tailnet-name.ts.net). The device name comes from
     `tailscale status`, NOT necessarily the Windows computer name — copy
     the exact URL `tailscale funnel 8080` prints.
   - **DNS propagation note:** the Funnel URL worked from the laptop
     immediately (laptop is on the tailnet → resolves via MagicDNS), but the
     Mac showed "site can't be reached" for the first few minutes (public DNS
     propagation). Resolved itself within ~10 minutes. Vercel's servers use
     public DNS, so callbacks may be delayed a few minutes after first
     creating a Funnel — not a real problem.
7. **Set `STATION_API_URL`** in the Vercel dashboard (Production + Preview) =
   the Funnel URL, then **Redeploy** (Production, "Use existing Build Cache"
   unchecked). After redeploy, the live site at `bambu-farm-web.vercel.app`
   shows the 4 printers' live status + cameras (confirmed by user) — so the
   status/camera path through the tunnel works end-to-end.
8. **Started the daemon** with `python main.py`. All 4 MQTT printers connect
   (`The Allee`, `The Room`, `The Forecourt`, `The Bust` — IPs
   192.168.1.172/89/111/169, serials 00M09A390900243 / 0938AC5C2300029 /
   00M00A342800191 / 00M09A3C0401135). Station API listens on
   `http://127.0.0.1:8080`. `Uvicorn running`. `GUI loop started`.
9. **End-to-end test (fresh upload):** uploaded
   `BROKER-2-reachy-mini-1.gcode.3mf` as `Miguel` targeting
   `00M09A390900243`. The **webhook path works in real time** — the laptop
   logged `enqueued blob` within seconds (NOT just catch-up), confirming
   Vercel → `onUploadCompleted` → Funnel → `/api/blob-webhook` → file_watcher
   queue. So the callback plumbing is fully live.

### Bugs found Jul 29 and FIXED (committed in `0b9045f`, pushed to `origin/main`)

Two real defects in the Python code blocked the daemon from running. Both
were fixed in the working tree on Jul 29 and committed in `0b9045f` (pushed
to `origin/main` the same day).

**Bug A — `NameError: name 'log' is not defined` in `main.py`
`_start_station_services`.** The function uses `log` in five places (warning /
info / exception calls) but never defines it. `log` is a local in `main()`, not
module-level, so it's out of scope. The first crash appeared as `httpx` missing
(the `except ImportError` block hit the `NameError` and masked the real import
error); after installing `httpx`, the crash moved to the
`log.info("Station API listening...")` line and became a hard blocker for the
station API. **Fix applied (working tree):** added
`log = logging.getLogger("main")` at the top of `_start_station_services`,
right after the docstring's closing `"""` and before `services: list = []`.
One line, fixes all five usages. `logging` is already imported at module top.

**Bug B — `IndexError: No item with that key` in
`station/file_watcher.py` `_already_processed`.** It does `row["blob_key"]` on
a `sqlite3.Row`, but `database.py`'s `list_recent_uploads` SELECT did NOT
include the `blob_key` column — so `row["blob_key"]` raised `IndexError`. This
blocked BOTH the startup catch-up `list` path AND the webhook-driven
`_process_blob` path (both call `_already_processed`). It only didn't crash on
the very first run because the `uploads` table was empty (the `for row in rows`
loop never executed); once any row existed, every blob processing attempt
crashed. **Fix applied (working tree):** added `blob_key` to the SELECT column
list in `list_recent_uploads` (right after `upload_id`). One line.

Both fixes verified with `python -m py_compile main.py database.py` → "both
compile OK". The daemon now starts and stays up; the catch-up list and webhook
path no longer crash on `_already_processed`.

### BLOCKER found Jul 29 — 403 Forbidden downloading private blobs (RESOLVED Jul 30)

> Resolved Jul 30 by adding Bearer auth to the GET in `_download_blob` — see
> "Status of prior blockers" in the current §0 above. The full diagnosis
> below is preserved for history.

After the two bug fixes, the daemon runs and the webhook path fires in real
time (`enqueued blob` appears within seconds of an upload). BUT the
**download step fails with HTTP 403** for every blob:

```
httpx: HTTP Request: GET https://gsbgh5itt9onw0ev.private.blob.vercel-storage.com/incoming/...?download=1 "HTTP/1.1 403 Forbidden"
ERROR station.file_watcher: Upload failed for incoming/...: Client error '403 Forbidden'
```

**Root cause:** the `bambu-submissions` Blob store is **Private**. Private
blobs are NOT fetchable via their plain `url`
(`https://...private.blob.vercel-storage.com/...`) — they need a **signed
`downloadUrl`**. `file_watcher.py` `_download_blob` correctly prefers
`downloadUrl` (`url = blob.get("downloadUrl") or blob.get("url")`), but it's
hitting the plain `url` (→ 403), which means **`downloadUrl` is missing** from
the blob dict. This happens in TWO scenarios observed this session:

- **Old/stale blobs** (uploaded Jul 21, 8 days ago): signed `downloadUrl`s
  expire in ≤7 days. The catch-up `list` returned these old blobs without a
  fresh signed `downloadUrl`. The user DELETED these 2 stale test blobs
  (`Miguel_Ramirez_Peraza__any__body_down_3print.gcode.3mf` and
  `...rods.gcode.3mf`) from the Vercel dashboard to clear them.
- **Fresh upload (Jul 29):** the webhook path STILL 403'd on `...?download=1`.
  This means the `downloadUrl` is ALSO missing/invalid on the webhook path —
  i.e. the `onUploadCompleted` callback is either not forwarding
  `downloadUrl`, OR the `BlobWebhookPayload` in `station/api_server.py` isn't
  capturing it, OR the signed URL Vercel generates isn't usable. **This is the
  live blocker — fresh uploads cannot be downloaded.** The blob sits in Vercel
  Blob (not deleted), the `uploads` row is marked `failed`, and nothing lands
  in `C:\BambuSubmissions\`.

**Why the docs thought this would work:** old `AGENTS.md` §7 Issue #4 and
`web/app/api/upload/route.ts` both say `onUploadCompleted` forwards
`{pathname, url, downloadUrl}` and `api_server.py`'s `BlobWebhookPayload`
"gained optional `downloadUrl`". So the design intent was for the webhook
payload to carry a fresh signed `downloadUrl`. The 403 on a fresh upload means
somewhere in that chain the `downloadUrl` is being dropped, expired, or the
private-store signed URL isn't being honored. **Needs investigation in the
next chat.**

**The fix direction (NOT applied — user said do this later):** two options,
either or both:
1. **Add Bearer auth to the GET in `_download_blob`** so the `url` fallback
   works for private stores (private blobs ARE readable with the
   `BLOB_READ_WRITE_TOKEN` in the `Authorization: Bearer` header). This is the
   "optional hardening" mentioned in old AGENTS.md §7 Issue #4 that was never
   implemented. It would make the system robust to missing/expired
   `downloadUrl` regardless of the webhook path. ~3-line change.
2. **Investigate why `downloadUrl` is missing on the webhook path** — check
   `web/app/api/upload/route.ts` `onUploadCompleted` actually sends
   `downloadUrl`, and `station/api_server.py` `BlobWebhookPayload` actually
   parses it, and `file_watcher.enqueue_blob` actually stores it on the queue
   item, and `_process_blob`/`_download_blob` actually receive it. A field may
   be getting dropped somewhere in the chain.

**Do NOT delete more blobs or "just retry" — the 403 reproduces on fresh
uploads, so this is a real bug to fix in code, not a stale-data issue.**

### State summary as of Jul 29 (superseded — see current §0 above)

> This was the state at the end of the Jul 29 session. The two bugs and the
> 403 blocker listed here have since been resolved (Jul 29/30). Preserved
> for history; for the current state see "Where we are right now (Jul 30)"
> at the top of this file.

- ✅ Laptop set up: code cloned, deps installed, `config.json` filled,
  `C:\BambuSubmissions` created, Tailscale Funnel up (persistent, survives
  reboot), daemon running with all 4 printers connected.
- ✅ Vercel `STATION_API_URL` set + redeployed; live site shows printer
  status + cameras (status/camera path through tunnel works).
- ✅ Webhook path fires in real time (`enqueued blob` within seconds of
  upload) — the callback plumbing is live.
- ✅ Two Python bugs fixed in the working tree (main.py `log`,
  database.py `blob_key`) — daemon stays up, catch-up + webhook no longer
  crash on `_already_processed`. (Committed in `0b9045f` Jul 29.)
- ❌ ~~Download step 403s on private blobs~~ → **RESOLVED Jul 30** (Bearer
  auth on the GET in `_download_blob`).
- ❌ ~~The two bug fixes are NOT yet committed/pushed~~ → **RESOLVED Jul 29**
  (committed in `0b9045f`, pushed to `origin/main`).

### What was left at end of Jul 29 (superseded — see current §0 "What's left")

> This was the to-do list at the end of the Jul 29 session. Items 1 and 2
  have since been done (Jul 29/30). Preserved for history; for the current
  to-do list see "What's left (next chat)" in the current §0 above.

1. **Fix the 403 download blocker** (the user explicitly deferred this to
   the next chat). Start by deciding between the two fix directions above;
   the Bearer-auth-on-GET fix (option 1) is the most robust and ~3 lines.
   Then verify a fresh upload lands in `C:\BambuSubmissions\<name>\` and the
   blob is deleted from Vercel. → **DONE Jul 30** (option 1 applied).
2. **Commit + push the bug fixes** to GitHub: `main.py` (`log` fix),
   `database.py` (`blob_key` fix), and the `_download_blob` 403 fix once
   applied. `main` is protected — pushes need user confirmation. Before
   pushing, scan `git diff --cached` for real secrets (the user's
   `BLOB_READ_WRITE_TOKEN` value was visible in a screenshot saved to the
   workspace this session — do NOT commit any screenshot containing it; if
   the workspace is ever shared, regenerate the token in Vercel). →
   **DONE Jul 29** (the two bugs in `0b9045f`); the 403 fix is being
   committed this session (Jul 30).
3. **(Optional) Make the Python daemon survive reboot** via
   `install_autostart.bat` (the Tailscale Funnel already survives reboot
   via the `tailscaled` service). Not yet done.
4. **(Optional) Update `station/TUNNEL.md`** to document the Tailscale
   Funnel path as a first-class alternative (the doc currently only covers
   Cloudflare Tunnel + Tailscale Funnel/ngrok as brief "alternatives"). The
   user is actually running Tailscale Funnel, so the doc should reflect
   that the Tailscale path is the live one.

### Prior session (Jul 21, 2026) — web side, for background

- Vercel dashboard redeploy fixed "No token found"; then diagnosed + fixed
  "Cannot use public access on a private store" by upgrading `@vercel/blob` to
  `^2.6.1` and setting `access: "private"`. Pushed (`5d7d9ae` + `de001cb`).
- Implemented the client-side upload rewrite (Issue #1): browser uploads
  directly to Vercel Blob via `handleUpload`/`upload()` from
  `@vercel/blob/client`, bypassing the 4.5 MB serverless body limit. Pushed
  (`09f1949` + `8fcab36`) and redeployed via `npx vercel --prod`. Changes:
  `web/app/api/upload/route.ts` (`handleUpload` + `onUploadCompleted`
  forwarding `{pathname, url, downloadUrl}`), `web/components/UploadForm.tsx`
  (`upload()` with `access: "private"`, `multipart: true`, progress bar),
  `web/lib/blob.ts` (`buildPathname`), `station/api_server.py`
  (`BlobWebhookPayload` gained optional `downloadUrl`),
  `station/file_watcher.py` (`_download_blob` prefers `downloadUrl`).
- **Phase 1 COMPLETE:** a large `.gcode.3mf` upload succeeded on the live
  site; 0% error rate; big uploads work for $0 on Hobby.
- All four Jul 21 commits are on `origin/main` and deployed. To roll back the
  client-upload change only: `git reset --hard de001cb`.

---

## 1. What this project is

A custom Python daemon that does two things on a **dedicated Windows laptop** in a
Cornell Tech MakerLAB:

1. **Print labeling (original, working for months):** monitors a fleet of Bambu
   Lab printers over MQTT/TLS, logs every print job to a local SQLite database
   (`print_dataset.db`), and pops up a Tkinter prompt asking "Did this print
   fail?" when each job ends. Builds a labeled dataset for a future print-failure
   prediction model.
2. **Student submission (new, in progress):** students visit a public Vercel
   website, upload a sliced `.gcode.3mf` file, and the laptop pulls it down and
   archives it into an organized inbox folder. The student then walks into the
   lab, opens the official **Bambu Farm Manager (BFM)** app, and clicks Upload
   to send the file to a printer. The "send to printer" step stays a deliberate,
   in-lab human action — students never print from home.

This daemon is **not** the official Bambu product called "Bambu Farm Manager"
(BFM). Both run on the same laptop; they are separate pieces of software.

---

## 2. Architecture (student submission flow)

```
student (home) → Vercel Next.js site (web/) → /api/upload → Vercel Blob (incoming/)
                       ↓ /api/upload best-effort POSTs {pathname,url}
                         to station /api/blob-webhook (x-station-key shared secret)
station laptop → file_watcher downloads blob by URL, renames to
                 <name>_<file>.gcode.3mf, uploads to a printer via printer_uploader,
                 deletes the blob, archives the renamed file to
                 C:\BambuSubmissions\<sanitized name>\
student (in lab) → BFM Client → Upload → picks file from inbox → Create / Direct to Print
```

**Webhook-driven, NOT polling.** Vercel Blob counts every `list` as an advanced
operation (2,000/month max on the free Hobby plan); a 10s poll loop would
exhaust the quota in hours. So the laptop processes from a queue fed by the
webhook endpoint, does ONE catch-up `list` on startup for anything uploaded
while offline, and has an optional rare fallback poll (`web.poll_interval_s`,
default `0` = off). Do NOT reintroduce continuous polling.

Status + camera on the Vercel site are read from a small FastAPI server on the
laptop (`station/api_server.py`), exposed to the internet via a Cloudflare
Tunnel (`station/TUNNEL.md`). The laptop fetches printer telemetry over MQTT
(existing `mqtt_listener.py`) and camera JPEGs over the LAN.

---

## 3. Key files

### Python (laptop daemon)
- `main.py` — entry point; loads config, starts one PrinterMonitor per printer,
  and (optionally, when the `web` config section is present) starts the station
  API server + file watcher in daemon threads.
- `mqtt_listener.py` — `PrinterMonitor` class; MQTT connection + state machine;
  added `snapshot()` + `camera_url()` + thread lock for the API.
- `database.py` — SQLite helpers; `print_jobs` table (labeling) + `uploads`
  table (submissions).
- `labeler_gui.py` — Tkinter print-result popup. As of Jul 30 the question
  reads "How did this print turn out?" with buttons "Print Succeeded" (green)
  and "Print Failed" (whiteish) — the old inverted Yes/No wording ("Did this
  print fail?" → YES=bad) was replaced to remove confusion. The `0`=succeeded
  / `1`=failed label mapping is unchanged.
- `station/api_server.py` — FastAPI: `GET /api/printers`,
  `GET /api/printer/<serial>/camera`, `GET /api/health`, and
  `POST /api/blob-webhook` (shared-secret auth via `x-station-key` header,
  enqueues `{pathname,url}` on the watcher).
- `station/file_watcher.py` — **webhook-driven** Blob→printer bridge.
  `enqueue_blob()` feeds a queue; `_run()` processes it; `start()` does a
  single catch-up `list`; optional rare fallback poll. Downloads blob by URL
  (a cheap read, not a `list`), renames, uploads to printer, deletes blob,
  archives to per-student inbox subfolder.
- `station/camera.py` — fetches a JPEG snapshot from a printer over the LAN.
- `station/printer_uploader.py` — Bambu HTTP chunked upload client. **Deprecated
  in the decided workflow** (the human uploads via BFM), but still called by
  `file_watcher` to push the file to the printer's Files tab. Keep for reference.
- `station/inspect_bfm.py` — read-only BFM internals inspection tool (reference
  for the optional future "automate the last click" upgrade).
- `station/TUNNEL.md` — Cloudflare Tunnel setup.
- `config.json.template` — sanitized config template (committed). Real
  `config.json` is gitignored.

### Web (Vercel Next.js site, in `web/`)
- `web/app/api/upload/route.ts` — **client-side upload route** using
  `handleUpload` from `@vercel/blob/client`. `onBeforeGenerateToken` validates
  the pathname (name non-empty, `.gcode.3mf` extension) and caps size at 250 MB;
  `onUploadCompleted` (called by Vercel Blob after the browser's direct upload)
  best-effort POSTs `{pathname, url, downloadUrl}` to the laptop's
  `/api/blob-webhook` with the `x-station-key` secret. The file never passes
  through the function, so the 4.5 MB body limit doesn't apply.
- `web/lib/blob.ts` — `buildPathname()` (+ exported `sanitize`/`normalizeExt`)
  builds `incoming/<name>__<target>__<file>.gcode.3mf`, shared by client + server.
  `putSubmission()` (server-side `put()` with `access: "private"`) is kept as a
  fallback but is no longer used on the main path. **Requires `@vercel/blob`
  >= 2.3** (private-access + client-upload support); project is on `^2.6.1`.
- `web/components/UploadForm.tsx` — the upload form (name, target printer, file).
  Uses `upload()` from `@vercel/blob/client` with `access: "private"`,
  `multipart: true`, `handleUploadUrl: "/api/upload"`, and a progress bar.
- `web/components/PrinterCard.tsx`, `StatusBadge.tsx` — status grid UI.
- `web/lib/station.ts` — fetches printer status + camera from the station API.
- `web/.env.example` — documents `STATION_API_URL`, `STATION_WEBHOOK_SECRET`,
  `BLOB_READ_WRITE_TOKEN`.

---

## 4. Config (`config.json`, gitignored — never commit)

```json
{
  "printers": [ { "name", "ip", "serial", "access_code", "camera" } ],
  "web": {
    "blob_token":         "<Vercel Blob read/write token>",
    "station_api_url":    "https://makerlab-status.yourdomain.com",
    "webhook_secret":     "<openssl rand -hex 32 output>",
    "api_host":           "127.0.0.1",
    "api_port":           8080,
    "poll_interval_s":    0,
    "inbox_dir":          "C:\\BambuSubmissions"
  }
}
```

`api_host` `127.0.0.1` = only the tunnel reaches the API from outside (safer).
`poll_interval_s` `0` = webhook-driven, no continuous polling.

---

## 5. Secrets (must match between Vercel and the laptop)

| Vercel env var | laptop `config.json` key | notes |
|---|---|---|
| `BLOB_READ_WRITE_TOKEN` | `web.blob_token` | same Vercel Blob token in both |
| `STATION_WEBHOOK_SECRET` | `web.webhook_secret` | `openssl rand -hex 32`, generated once, identical both sides |
| `STATION_API_URL` | `web.station_api_url` | the laptop's Cloudflare Tunnel hostname |

Real secrets never go in the repo. `config.json.template` and `web/.env.example`
contain only placeholders.

---

## 6. Deployment status (as of Jul 30, 2026)

### GitHub
- Repo: https://github.com/miguel200-collab/Bambu-FM-Print-Data
- Branch: `main` (protected — pushes require user confirmation)
- On `origin/main` (deployed): the Jul 21 client-upload commits
  (`5d7d9ae`, `de001cb`, `09f1949`, `8fcab36`, `328e30d`) plus the Jul 29
  startup-bug-fix commit `0b9045f` (`main.py` `log`, `database.py`
  `blob_key`). The Jul 30 403-fix + popup-fix commit is being added this
  session (see §0).

### Vercel site
- Project: `bambu-farm-web` (Hobby plan, account `ramirezperazamiguel-7620s-projects`)
- URL: https://bambu-farm-web.vercel.app
- Deployed via **CLI** (`npx vercel --prod` from `web/`). Git repo NOT connected
  (optional; if connected later, set Settings → General → Root Directory = `web`).
- Env vars set in dashboard: `BLOB_READ_WRITE_TOKEN`, `BLOB_STORE_ID`,
  `BLOB_WEBHOOK_PUBLIC_KEY`, `STATION_WEBHOOK_SECRET`, and `STATION_API_URL`
  (Production + Preview). `STATION_API_URL` set this session (Jul 29) to the
  Tailscale Funnel URL.

### Vercel Blob store
- Store: `bambu-submissions` (Private, region IAD1), connected to `bambu-farm-web`
  for Production + Preview.

### Laptop (dedicated Windows machine)
- **SET UP Jul 29.** Running the new code at `C:\Bambu-FM-Print-Data`.
- Tunnel: **Tailscale Funnel** (not Cloudflare) — `tailscale funnel --bg 8080`,
  persistent via the `tailscaled` Windows service. Funnel URL:
  `https://desktop-rck343r.tailfc0d71.ts.net` (set as Vercel `STATION_API_URL`).
- Daemon running with all 4 printers connected; station API on `127.0.0.1:8080`.
- **403 download blocker FIXED Jul 30** (Bearer auth on the GET in
  `_download_blob`, committed this session) — see §0. **Awaiting end-to-end
  verification on the laptop** (`git pull` → restart → fresh upload → confirm
  archive + blob deletion). The two Jul 29 startup bugs were fixed and pushed
  in `0b9045f` Jul 29.

---

## 7. Known issues / blockers (current)

### Issue #1 — Vercel 4.5 MB body limit (RESOLVED, deployed)
Vercel enforces a hard 4.5 MB request body limit on all serverless functions,
on every plan, not increasable. The old `web/app/api/upload/route.ts` did a
**server-side** `put()` (file through the function), so any real `.gcode.3mf`
(10–50 MB) was rejected with HTTP 413 (`FUNCTION_PAYLOAD_TOO_LARGE`).

**Fix (deployed):** switched to **client-side uploads** using
`handleUpload`/`upload()` from `@vercel/blob/client`. The browser uploads
directly to Vercel Blob with a short-lived client token (max 250 MB, multipart
for >100 MB), so the file never passes through the function and the 4.5 MB limit
doesn't apply. The route's `onUploadCompleted` callback (fired by Vercel Blob
after the upload) forwards `{pathname, url, downloadUrl}` to the laptop's
`/api/blob-webhook`. Stays free on Hobby (see §8 cost notes). `npm run build`
passes; deployed via `npx vercel --prod`; a large `.gcode.3mf` upload succeeded
on the live site (Phase 1 complete).

### Issue #2 — "No token found" on the live site (RESOLVED)
The production deployment was built BEFORE the Blob store env vars were added,
so it was running without `BLOB_READ_WRITE_TOKEN` and uploads failed with
`Vercel Blob: No token found...`. **Resolved Jul 21, 2026** by a Vercel
dashboard redeploy (Build Cache unchecked) — confirmed by the next error
changing to Issue #4's "Cannot use public access on a private store."

### Issue #4 — Private store vs. old SDK (RESOLVED, deployed)
The `bambu-submissions` store is **Private**, and Vercel Blob access modes
**cannot be changed after store creation**. The pinned `@vercel/blob@^0.27.0`
only typed `access: 'public'`, so uploads failed with "Cannot use public access
on a private store" and a first `access: "private"` attempt broke `next build`.
**Resolved** by upgrading `@vercel/blob` to `^2.6.1` (`de001cb`) + setting
`access: "private"` (`5d7d9ae`); pushed + deployed; a small file then landed
under `incoming/`.

**Downstream note (handled Jul 30):** private blobs are **not downloadable via
their plain `url`** — the signed `downloadUrl` (valid ≤ 7 days) is what the
laptop was originally meant to GET. `station/api_server.py` accepts
`downloadUrl` in the webhook payload, and `station/file_watcher.py`
`_download_blob` prefers `downloadUrl` over `url` (the canonical `url` is
still used for the authenticated DELETE). In practice Vercel's
`onUploadCompleted` callback did NOT forward a usable `downloadUrl` on the
webhook path, so the daemon fell back to the plain `url` and 403'd.
**Resolved Jul 30** by sending `Authorization: Bearer <blob_token>` on the
GET in `_download_blob` — this makes the plain `url` fetchable for private
stores (the header is harmless when a valid signed `downloadUrl` is used),
and also rescues old blobs in the catch-up `list` whose signed URLs expired.
This supersedes the earlier "fall back to a list for a fresh signed URL"
idea — Bearer auth is simpler and more robust.

### Issue #3 — Laptop not set up (RESOLVED Jul 29; 403 blocker RESOLVED Jul 30)
The laptop was set up Jul 29 and is running the new code (Tailscale Funnel,
not Cloudflare Tunnel — see §0 and §6). The webhook path fires in real time.
The download step 403'd on private blobs (diagnosed Jul 29, see "Prior
session (Jul 29)" §0 for the full diagnosis) — **RESOLVED Jul 30** by adding
Bearer auth to the GET in `_download_blob` (committed this session).
**Awaiting end-to-end verification on the laptop** — see §0 "What's left".

---

## 8. The plan

### Phase 1 — big uploads live (DONE, Jul 21, 2026)
Client-side upload rewrite (`09f1949` + `8fcab36`) pushed, deployed via
`npx vercel --prod`, and verified with a large `.gcode.3mf` upload on the live
site. 0% error rate. Big uploads now work for $0 on Hobby. (Optional: confirm the
`onUploadCompleted` callback in the Vercel **Logs** tab — see §0.)

### Cost (Hobby, stays free)
- 4.5 MB function body limit: bypassed (browser → Blob direct).
- Blob storage: 1 GB cumulative. `file_watcher` deletes each blob after
  processing, so storage stays low; risk only if the laptop is offline for days
  during a busy period.
- Bandwidth: 100 GB/month (each submission = one Blob→laptop download) — plenty.
- Function invocations: 2 per upload (token + completion callback); 1M/month.
- Advanced ops (list): webhook-driven design keeps `list` rare (one catch-up on
  startup). Client uploads add zero `list` ops.
- No overage charges on Hobby — exceeding a limit suspends Blob until the next
  30-day window (worst case is a pause, not a bill).

### Then — set up the laptop (Issue #3)
5. Back up `config.json` + `print_dataset.db`; stop old daemon; `git clone`;
   restore the two files; `pip install -r requirements.txt`; add the `web`
   section to `config.json` (with `blob_token`, `webhook_secret`,
   `station_api_url`, `inbox_dir`); `mkdir C:\BambuSubmissions`; set up the
   Cloudflare Tunnel (`station/TUNNEL.md`); `python main.py`.
6. Set `STATION_API_URL` in the Vercel dashboard to the tunnel hostname and
   redeploy.
7. End-to-end test: upload a real `.gcode.3mf` → laptop logs `enqueued blob` →
   `Uploaded ... and archived to C:\BambuSubmissions\<name>\` → blob deleted
   from Vercel Blob.

### Optional later
- Connect the GitHub repo to Vercel (set Root Directory = `web`) for auto-deploy.
- Add auth to the upload form (currently anyone with the URL can submit).
- Add a shared-secret or Cloudflare Access gate to the station API.
- Fully automate the BFM "Upload" click (UI automation via pywinauto, or Bambu
  cloud API if files sync to bambulab.com) — see README "Optional future
  upgrade".

---

## 9. Conventions

- **Never commit secrets.** `.gitignore` covers `config.json`,
  `print_dataset.db` (+ `-shm`/`-wal`), `makerlab.log`, `incoming/`,
  `processed/`, `BambuSubmissions/`, `web/.env.local`, `web/.next/`,
  `web/node_modules/`, `web/.vercel/`. Before pushing, verify with
  `git diff --cached | grep` for real secret patterns; only placeholders
  (`REPLACE_ME`, `YOUR_*`, `replace_with_*`) should appear.
- **`main` is a protected branch.** Pushes require explicit user confirmation.
- **Keep the webhook-driven design.** Do not reintroduce continuous polling —
  it would exhaust the Hobby Blob operation quota.
- **The BFM "send to printer" click stays manual** (decided "Pivot A"). Don't
  try to reverse-engineer BFM's upload API; that's an optional future upgrade.
- **Python 3.11+**, dependencies in `requirements.txt` (`paho-mqtt`, `fastapi`,
  `uvicorn[standard]`, `httpx`). Web is Next.js 14 + `@vercel/blob`.
- After pulling new code on the laptop, just restart the daemon — don't delete
  `config.json`, `print_dataset.db`, or `makerlab.log`.

---

## 10. Quick links

- Official BFM docs: https://wiki.bambulab.com/en/software/bambu-farm-manager
- Tunnel setup: `station/TUNNEL.md`
- Vercel Blob client uploads (for Issue #1 fix):
  https://vercel.com/docs/vercel-blob/using-blob-sdk
- Vercel 4.5 MB body limit:
  https://vercel.com/docs/functions/limitations
- Inspection script (reference): `station/inspect_bfm.py`
- Web app setup: `web/README.md`
