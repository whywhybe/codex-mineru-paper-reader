# Setup and operations

Requires Python 3.10+ and requests (`python -m pip install requests` if absent).

On Windows, run `scripts/configure-token.ps1` in your own PowerShell window. On other platforms, supply `MINERU_API_TOKEN` through your local environment or secret manager; the DPAPI setup script is Windows-only. It prompts without echo and saves a Windows DPAPI encrypted credential for the current Windows user. No token should be pasted into chat or command arguments. The helper first checks MINERU_API_TOKEN, then this encrypted credential. Re-run configuration to rotate it.

Default cache without local configuration: `~/Documents/Codex/MinerU-Library`. An optional skill-root `config.json` can set `cache_root`. Override with MINERU_CACHE_ROOT or --cache-root. The cache stores result.zip, extracted mineru_raw output, manifest.json, and an original-PDF reference or fallback snapshot as described below. Signed upload/download URLs and URL query strings are not persisted. Source URLs with queries are recorded without query credentials, so exact re-download may require the original source link.

## Original-PDF storage and recovery

During submission and pending/failed tasks, preserve the input snapshot `source.pdf`. For newly completed tasks, compare PDFs inside result.zip with the input SHA-256, without relying on their names. Only an exact match permits deletion of redundant cache PDFs:

- Local input: keep the user's input untouched; record its absolute path and hash. Remove matching standalone cache copies, including extracted originals, while keeping result.zip as backup. If the external file has already changed by completion, retain one verified extracted original instead.
- Network input: keep one matching original in mineru_raw and remove the outer source.pdf and additional byte-identical extracted originals.
- ZIP missing the original or containing a different PDF: keep source.pdf. Other PDFs such as annotated layouts are never removed just because they have a PDF extension.

Use `python <skill>/scripts/reader.py --original <cache_id> --cache-root <cache-root>` to find the parsed original. This works without the old input path, a download, an API request, or a Token. It validates the external local file when present; otherwise it uses the preserved snapshot/extracted original, or restores the verified ZIP member to `restored-original.pdf`. Changed user files are never overwritten. The recovered copy is retained for later use. A modified ZIP or cached result is reported as CACHE_DAMAGED rather than silently trusted.

The manifest records `source_kind`, `local_source` for local inputs, and `original` with its ZIP member and current cached path. Hashes cover retained cache files; the external original is separately checked against the input SHA-256. Cleanup intent is saved before deleting duplicate files so interrupted cleanup can resume. Previous completed caches keep their existing layout and are not mass-cleaned; copies exported to another machine can restore from the ZIP if the original local path is unavailable.

The 2026-09-30 official example ZIP contained an origin PDF with different bytes from the uploaded PDF. The 2026-10-02 offline replay therefore correctly preserved source.pdf. Do not promise deduplication for every MinerU result.

Use `--wait 45` for bounded polling. Repeating the command resumes a saved batch. A crashed process can leave a lock: inspect its PID and confirm it is no longer active before removing only that lock. Do not remove locks belonging to active processes.

API POST is deliberately not blindly retried. If submission was accepted but its response lost, state is submission_unknown: reconcile with MinerU's account dashboard before resetting. If a batch ID exists but upload acknowledgement was lost, use --recover to query that batch; do not upload a second document automatically. If it remains waiting-file, the upload needs manual reconciliation. Failed tasks are retained; use offline `--archive-failed <cache_id>`, then explicitly link the next attempt with `--retry-from <archive-name>`. See [diagnostics and recovery](diagnostics.md) for JSONL locations, error categories, redaction and ZIP recovery. Never describe a failed task as done.

The API currently documents a 200 MB / 200 page per-file limit. The helper checks byte size and PDF signature; server validates page limits. Oversize-page PDFs require an explicitly planned split with original-page mapping; do not silently truncate. API documentation checked 2026-09-30: https://mineru.net/apiManage/docs . Local-file flow: POST /api/v4/file-urls/batch, PUT signed upload URL without bearer token, GET /api/v4/extract-results/batch/{batch_id}, download ZIP without bearer token. Documentation disagrees on batch count (overview 200 vs detailed 50); helper submits one PDF per batch.

Project export: copy the project and the directories referenced by literature/index.json into an export cache/documents directory; set MINERU_CACHE_ROOT to that export cache on the receiving machine. Preserve manifests and hashes. Cache root migration is a copy/move plus updating MINERU_CACHE_ROOT, not rewriting project IDs. Never automatically prune global cache when deleting a project.

Actual reading notes should record cache_id, source version, sections read, figures/tables checked and unresolved discrepancies. The parser does not synthesize page mappings or reading claims. This version uses browsing by Codex for DOI/title resolution; the command accepts direct PDFs only.

