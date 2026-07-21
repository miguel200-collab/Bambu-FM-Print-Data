# AGENTS.md — Project state for new chats

> Read this first. It captures what the project is, what's built, what's
> deployed, what's broken right now, and the plan. Keep it updated as work
> progresses so the next chat starts on the same page.

---

## 0. Where we are right now (Jul 21, 2026, ~11:30 ET)

**Goal of this session:** make the live upload form accept real (large)
`.gcode.3mf` files, keep it free on the Hobby plan, and update the docs.

**Done this session:**
- Vercel dashboard redeploy (Build Cache unchecked) — fixed Issue #2 ("No token
  found"). Confirmed by the error changing to Issue #4.
- Diagnosed + fixed Issue #4: Private store + `@vercel/blob@^0.27.0` (only typed
  `access: 'public'`) → "Cannot use public access on a private store". Upgraded
  `@vercel/blob` to `^2.6.1` and set `access: "private"`. Pushed (`5d7d9ae` +
  `de001cb` on `origin/main`) and redeployed — a small file (`rods.gcode.3mf`)
  then landed successfully under `incoming/`.
- Implemented the client-side upload rewrite (Issue #1): the browser now uploads
  directly to Vercel Blob via `handleUpload`/`upload()` from `@vercel/blob/client`,
  bypassing the 4.5 MB serverless body limit. Changes (committed locally, NOT yet
  pushed):
  - `web/app/api/upload/route.ts` — `handleUpload` route: `onBeforeGenerateToken`
    validates the pathname + caps size at 250 MB; `onUploadCompleted` forwards
    `{pathname, url, downloadUrl}` to the laptop's `/api/blob-webhook`.
  - `web/components/UploadForm.tsx` — uses `upload()` with `access: "private"`,
    `multipart: true`, and a progress bar; builds the pathname client-side.
  - `web/lib/blob.ts` — added `buildPathname` (+ exported `sanitize`/`normalizeExt`);
    `putSubmission` kept as a server-side fallback.
  - `station/api_server.py` — `BlobWebhookPayload` gained optional `downloadUrl`;
    enqueued blob carries it.
  - `station/file_watcher.py` — `_download_blob` now prefers `downloadUrl` (signed)
    over `url` (private stores need the signed URL for GET; `url` is still used for
    the authenticated DELETE).
- Verified `npm run build` passes locally with all web changes.

**Next actions (blocked on user):**
- `git push origin main` (protected) — push the new client-upload commit(s).
- `cd web && npx vercel --prod` to redeploy.
- Test upload with a **large** `.gcode.3mf` (>4.5 MB); expect a blob under
  `incoming/` in Storage → `bambu-submissions`, and (if the laptop/tunnel is up)
  laptop logs `enqueued blob` → `Uploaded ... and archived to C:\BambuSubmissions\<name>\`.
- Then set up the laptop (Issue #3) if not already: see §8 step 6.

**If something went wrong / how to recover:**
- The previously-pushed private-store fix (`5d7d9ae` + `de001cb`) is on `origin/main`
  and live — small uploads already work. The new client-upload commit is local only;
  if its build breaks on Vercel, production stays on the previous deploy (Vercel
  keeps the old deployment live on build failure) — just fix and redeploy.
- To roll back the client-upload change only: `git reset --hard de001cb` (keeps the
  private-store fix, discards the client-upload rewrite).
- Private-store download caveat: signed `downloadUrl`s expire (≤7 days). The laptop
  uses them for GET and the canonical `url` for DELETE. If a webhook-delivered URL
  expires before the laptop processes it, the by-URL GET fails; the laptop's startup
  catch-up `list` returns fresh signed URLs. (Optional hardening: on GET failure,
  fall back to a `list` for a fresh signed URL — not yet implemented.)

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
- `labeler_gui.py` — Tkinter "Did this print fail?" popup (unchanged from
  original).
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

## 6. Deployment status (as of Jul 21, 2026)

### GitHub
- Repo: https://github.com/miguel200-collab/Bambu-FM-Print-Data
- Branch: `main` (protected — pushes require user confirmation)
- On `origin/main`: `de001cb` (Upgrade @vercel/blob to ^2.6.1) and `5d7d9ae`
  (Use private access for submissions). Both pushed + deployed; small uploads
  work.
- Pending push (local only): the client-side upload rewrite (Issue #1) —
  `handleUpload`/`upload()` rewrite of `web/app/api/upload/route.ts` +
  `web/components/UploadForm.tsx` + `web/lib/blob.ts`, plus the laptop-side
  `downloadUrl` handling in `station/api_server.py` + `station/file_watcher.py`.
  `npm run build` passes locally. Push + redeploy + test with a large file next.

### Vercel site
- Project: `bambu-farm-web` (Hobby plan, account `ramirezperazamiguel-7620s-projects`)
- URL: https://bambu-farm-web.vercel.app
- Deployed via **CLI** (`npx vercel --prod` from `web/`). Git repo NOT connected
  (optional; if connected later, set Settings → General → Root Directory = `web`).
- Env vars set in dashboard: `BLOB_READ_WRITE_TOKEN`, `BLOB_STORE_ID`,
  `BLOB_WEBHOOK_PUBLIC_KEY`, and `STATION_WEBHOOK_SECRET` (Production + Preview).
- `STATION_API_URL` not yet set (tunnel not created yet).

### Vercel Blob store
- Store: `bambu-submissions` (Private, region IAD1), connected to `bambu-farm-web`
  for Production + Preview.

### Laptop (dedicated Windows machine)
- **NOT set up yet.** Still running the OLD version (MQTT labeling only).
- Next steps to set it up: back up `config.json` + `print_dataset.db` → stop old
  daemon → `git clone` the repo → restore the two files → `pip install -r
  requirements.txt` (pulls fastapi/uvicorn/httpx) → add the `web` section to
  `config.json` → `mkdir C:\BambuSubmissions` → set up Cloudflare Tunnel →
  `python main.py`.

---

## 7. Known issues / blockers (current)

### Issue #1 — Vercel 4.5 MB body limit (RESOLVED, pending redeploy + test)
Vercel enforces a hard 4.5 MB request body limit on all serverless functions,
on every plan, not increasable. The old `web/app/api/upload/route.ts` did a
**server-side** `put()` (file through the function), so any real `.gcode.3mf`
(10–50 MB) was rejected with HTTP 413 (`FUNCTION_PAYLOAD_TOO_LARGE`).

**Fix (implemented, committed locally, pending push + redeploy + test):**
switched to **client-side uploads** using `handleUpload`/`upload()` from
`@vercel/blob/client`. The browser uploads directly to Vercel Blob with a
short-lived client token (max 250 MB, multipart for >100 MB), so the file
never passes through the function and the 4.5 MB limit doesn't apply. The
route's `onUploadCompleted` callback (fired by Vercel Blob after the upload)
forwards `{pathname, url, downloadUrl}` to the laptop's `/api/blob-webhook`.
Stays free on Hobby (see §8 cost notes). `npm run build` passes locally.

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

**Downstream note (partly handled):** private blobs are **not downloadable via
their plain `url`** — the signed `downloadUrl` (valid ≤ 7 days) is what the
laptop GETs. `station/api_server.py` now accepts `downloadUrl` in the webhook
payload, and `station/file_watcher.py` `_download_blob` prefers `downloadUrl`
over `url` (the canonical `url` is still used for the authenticated DELETE).
Remaining optional hardening: on a failed by-URL GET (expired signed URL), fall
back to a `list` for a fresh signed URL. Not yet implemented — low risk for a
single laptop with low volume.

### Issue #3 — Laptop not set up
No tunnel, no daemon running the new code. Submissions sit in Blob until the
laptop is set up and does its startup catch-up `list`.

---

## 8. The plan

### Immediate (Vercel) — push client-upload rewrite + redeploy + test (NEXT, Jul 21, 2026)
1. Push the client-side upload commit (local only) to `origin/main` (protected —
   needs explicit confirmation). `npm run build` passes.
2. Redeploy: `cd web && npx vercel --prod` (or dashboard Redeploy with Build Cache
   unchecked).
3. Test with a **large** `.gcode.3mf` (>4.5 MB) — confirm a blob appears in
   `bambu-submissions` under `incoming/`, and (if the laptop/tunnel is up) the
   laptop logs `enqueued blob` → `Uploaded ... and archived to
   C:\BambuSubmissions\<name>\` → blob deleted from Vercel Blob. If
   `STATION_API_URL` isn't set yet, the notify is skipped and the blob just sits
   in Blob until the laptop's catch-up `list` — still a pass for the upload test.
4. Verify the `onUploadCompleted` callback fired: check the `/api/upload`
   function logs for the station notify attempt. Requires `BLOB_WEBHOOK_PUBLIC_KEY`
   (already set) for callback verification.

### Done this session (for reference)
- Private-store fix (`5d7d9ae` + `de001cb`) — pushed + deployed; small uploads work.
- Client-side upload rewrite (Issue #1) — `handleUpload`/`upload()`, 250 MB cap,
  multipart, progress bar, `access: "private"`. Laptop webhook + `file_watcher`
  updated to carry/prefer the signed `downloadUrl`. Build passes. Pending push.

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
