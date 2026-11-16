# AGENTS.md

Checks and boundaries for this repo. The station runs in production as an NSSM service on a Windows laptop. Quality tooling must not change that behavior.

## Checks

From the repo root, with Python 3.11+:

```bash
python -m pip install -r requirements-dev.txt
ruff check .
mypy
pytest
```

Web (`web/`, Node 20):

```bash
cd web
npm ci
npm run lint
npm run typecheck
npm run build
```

`pytest` and the web unit tests (`npm test`) do not need printers or a network. They must not open the real `print_dataset.db`. `ruff format` is configured in `pyproject.toml` and is not a CI gate.

## Architecture

- `main.py` — process entry. Loads `config.json`, starts one MQTT monitor per printer, the headless event loop, and (when `web` is configured) the station API plus the file watcher. `--mock` writes `print_dataset_mock.db` and starts no HTTP server.
- `mqtt_listener.py` — per-printer MQTT/TLS session and the print state machine.
- `database.py` — SQLite (`print_dataset.db`). Migrations are additive only.
- `station/api_server.py` — FastAPI on `web.api_host`:`web.api_port` (production `127.0.0.1:8080`): printer status, camera proxy, blob webhook, label endpoints.
- `station/file_watcher.py` — webhook queue, one startup catch-up list, archive into `web.inbox_dir`, then delete the blob.
- `station/matching.py`, `station/families.py`, `station/gcode_features.py` — pure matching, design families, slicer features.
- `analysis/` — offline audit, backup, and feature backfill. Not on the request path.
- `web/` — Next.js site on Vercel. The browser uploads to Blob; server routes hold `STATION_WEBHOOK_SECRET` and call the laptop.

## Hard rules

- Do not change ports, filesystem paths, config keys, or startup entry points (`python main.py`, the NSSM service, `web/` on Vercel).
- Do not change runtime behavior to satisfy a linter. Widen a type, or ignore the finding, instead of rewriting a branch.
- Do not add comments whose only purpose is to justify a workaround.
- Every behavior fix comes with a test. Tests use a temporary database, never `print_dataset.db`, and do not contact printers or the network.
- Windows-only code must still import on Linux. Skip a test with a reason when it needs a Windows API; do not let that path fail CI.

## Left for later

Do not mass-rewrite these in a drive-by:

- Ruff `UP045` (`Optional[T]` to `T | None`) and `UP017` (`datetime.UTC`) are ignored in `pyproject.toml`.
- `zip` without `strict` stays in `analysis/audit.py` (pairwise `stamps` / `stamps[1:]`) and `station/families.py` (bbox sides). `strict=True` would raise when the lengths differ.
- `ruff format` is configured and not run in CI.
- Mypy is lenient on `main`, `database`, `mqtt_listener`, `station.api_server`, `station.file_watcher`, `station.printer_uploader`, and `tests`. Do not set `strict = true` in a mypy override; that flag turns strict on for the whole run. The strict modules list individual flags instead.
- Type-only `cast`s: `sqlite3.Row` passed to `pick_upload`, `json.load` in the backup config reader, the footprint-area `sum`, and the MQTT job id inside `_on_print_end`.
- `PrinterCard` still uses `<img>`. `next lint` warns. Do not switch it to `next/image` just to clear the warning.
