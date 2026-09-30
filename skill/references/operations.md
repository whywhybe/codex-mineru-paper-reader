# Setup and operations

Requires Python 3.10+ and requests (`python -m pip install requests` if absent).

On Windows, run `scripts/configure-token.ps1` in your own PowerShell window. On other platforms, supply `MINERU_API_TOKEN` through your local environment or secret manager; the DPAPI setup script is Windows-only. It prompts without echo and saves a Windows DPAPI encrypted credential for the current Windows user. No token should be pasted into chat or command arguments. The helper first checks MINERU_API_TOKEN, then this encrypted credential. Re-run configuration to rotate it.

Default cache without local configuration: `~/Documents/Codex/MinerU-Library`. An optional skill-root `config.json` can set `cache_root`. Override with MINERU_CACHE_ROOT or --cache-root. The cache stores original PDF, result.zip, untouched mineru_raw output, and manifest.json. Signed upload/download URLs and URL query strings are not persisted. Source URLs with queries are recorded without query credentials, so exact re-download may require the original source link.

Use `--wait 45` for bounded polling. Repeating the command resumes a saved batch. A crashed process can leave a lock: inspect its PID and confirm it is no longer active before removing only that lock. Do not remove locks belonging to active processes.

API POST is deliberately not blindly retried. If submission was accepted but its response lost, state is submission_unknown: reconcile with MinerU's account dashboard before resetting. If a batch ID exists but upload acknowledgement was lost, use --recover to query that batch; do not upload a second document automatically. If it remains waiting-file, the upload needs manual reconciliation. Failed tasks are retained; a fresh attempt requires deliberate archival of that failed cache directory after diagnosis. Never describe a failed task as done.

The API currently documents a 200 MB / 200 page per-file limit. The helper checks byte size and PDF signature; server validates page limits. Oversize-page PDFs require an explicitly planned split with original-page mapping; do not silently truncate. API documentation checked 2026-09-30: https://mineru.net/apiManage/docs . Local-file flow: POST /api/v4/file-urls/batch, PUT signed upload URL without bearer token, GET /api/v4/extract-results/batch/{batch_id}, download ZIP without bearer token. Documentation disagrees on batch count (overview 200 vs detailed 50); helper submits one PDF per batch.

Project export: copy the project and the directories referenced by literature/index.json into an export cache/documents directory; set MINERU_CACHE_ROOT to that export cache on the receiving machine. Preserve manifests and hashes. Cache root migration is a copy/move plus updating MINERU_CACHE_ROOT, not rewriting project IDs. Never automatically prune global cache when deleting a project.

Actual reading notes should record cache_id, source version, sections read, figures/tables checked and unresolved discrepancies. The parser does not synthesize page mappings or reading claims. This version uses browsing by Codex for DOI/title resolution; the command accepts direct PDFs only.

