# Bambu Farm Manager — Web (Vercel)

A futuristic, Cornell Tech-themed Next.js site where students see live printer
status + camera snapshots and submit sliced `.gcode.3mf` files. Submitted files
land in Vercel Blob; the dedicated laptop is notified via a webhook (no polling)
and drops each renamed file into an inbox folder on the laptop (see `../station/`
and the "Project context" section in `../README.md`).

## Setup

1. Install Node 18+.
2. `npm install`
3. Copy `.env.example` to `.env.local` and fill in:
   - `STATION_API_URL` — the public URL of the station's FastAPI server
     (exposed via a Cloudflare Tunnel; see `../station/TUNNEL.md`).
   - `STATION_WEBHOOK_SECRET` — shared secret sent in the `x-station-key` header
     when notifying the station's `/api/blob-webhook`. Must match
     `web.webhook_secret` in the laptop's `config.json`. Generate with
     `openssl rand -hex 32`.
   - `BLOB_READ_WRITE_TOKEN` — from your Vercel Blob store.
4. `npm run dev` (local) or push to Vercel.

## How submission works

- The browser uses **client-side uploads** (`upload()` from `@vercel/blob/client`):
  it `POST`s to `/api/upload` (this app's route handler, via `handleUpload`) to get
  a short-lived client token, then uploads the file **directly to Vercel Blob** at
  `incoming/<name>__<targetSerial>__<filename>.gcode.3mf`. Because the file never
  passes through the serverless function, Vercel's 4.5 MB request body limit does
  not apply — real 10–50 MB `.gcode.3mf` slices upload fine. A progress bar
  reflects the direct upload.
- When the upload completes, Vercel Blob calls back into `/api/upload`
  (`onUploadCompleted`), which best-effort notifies the station's
  `/api/blob-webhook` endpoint (over the Cloudflare Tunnel) with
  `{pathname, url, downloadUrl}` so the laptop downloads the blob immediately —
  **no polling**. If the tunnel/laptop is unreachable the blob stays safely in
  Blob, and the laptop grabs it via a single catch-up `list` on next startup. A
  failed notify never fails the upload.
- The Blob store is **Private**, so the laptop downloads each blob via the signed
  `downloadUrl` (valid ≤ 7 days) and deletes it via the authenticated canonical
  `url`. The `file_watcher` deletes each blob after processing, which keeps Blob
  storage well under the 1 GB Hobby limit under normal operation.
- The station's `file_watcher.py` downloads the blob, renames it to
  `<name>_<filename>.gcode.3mf`, and drops it into the laptop's inbox folder
  (e.g. `C:\BambuSubmissions\<student name>\`).
- **In the lab**, the student opens Bambu Farm Manager on the dedicated laptop,
  clicks **Upload**, browses to their folder in the inbox, and selects their
  file. BFM ingests it into its Files tab. The student then clicks
  **Create → Direct to Print** and picks a matching idle printer.
- The "target printer" dropdown on the upload form is optional metadata only —
  the human picks the printer in BFM, not from the website.

## How status works

- The page calls `GET <STATION_API_URL>/api/printers` at render time.
- Each printer card's camera `<img>` points at
  `<STATION_API_URL>/api/printer/<serial>/camera`, which the station proxies
  from the printer over the LAN.
