# Diagnostics and recovery

Every `reader.py` invocation creates an append-only JSONL file. No additional MinerU request is made for logging, credential validation or failure diagnosis. All times in the records are UTC ISO 8601; durations use a monotonic clock.

## Locate the records

- After the input is identified: `<cache-root>/logs/<cache_id>/<run_id>.jsonl`.
- Before a cache ID is available: `<cache-root>/logs/unassigned/<run_id>.jsonl`.
- The final CLI JSON includes `diagnostic_log`, including on errors when logging was possible.
- `documents/<cache_id>/manifest.json` includes `diagnostic_log` (latest), `diagnostic_logs` (history), `last_run`, `last_error`, and `output_files`.
- A failed remote task also has `remote_error` with the available `err_code`, `err_msg`, `data_id`, and `file_name`. A missing field is null, not an invented error code. A later blocked retry can update `last_error`; `remote_error` and the older event files remain available.
- Logs live outside the immutable output hash inventory; subsequent logging does not invalidate a completed cache. An archived failed manifest remains unchanged. Its logs remain under `logs/<cache_id>/`, so export that directory along with the archived attempt when sharing diagnostics.

`run_id` identifies one invocation; `attempt_id` identifies a submission attempt across polling invocations. Legacy manifests can have a null attempt ID; batch ID, cache ID and archived manifest hash still identify them. A new attempt's `previous_attempt` records the archive directory, old batch ID, old attempt ID when available, and old manifest SHA-256.

## Fields

| Fields | Meaning |
|---|---|
| `time`, `run_id`, `event`, `mode`, `pid` | UTC event time, invocation identity, event kind and operation |
| `stage`, `elapsed_ms`, `stage_elapsed_ms` | Current stage, total and stage elapsed time |
| `cache_id`, `attempt_id`, `batch_id` | Input/configuration identity, submission attempt, remote batch |
| `state`, `previous_state`, `remote_state` | Local state transition and reported remote state |
| `recover`, `retry_from`, `previous_batch_id`, `previous_manifest_sha256` | Poll-only recovery and explicit retry provenance |
| `category`, `message`, `exception_type` | Diagnostic category, controlled message and exception class; never a raw traceback |
| `http_status`, `api_code` | Observed HTTP status and API envelope code, when available |
| `err_code`, `err_msg`, `data_id`, `file_name` | Allowlisted, sanitized remote failure fields; missing fields remain null |
| `credential_source`, `credential_exists`, `process_returncode`, `dpapi_error_type`, `request_sent` | Credential helper metadata, not its output; the cryptographic exception name is recognized from stderr without saving stderr |
| `file`, `bytes`, `output_count` | Output inventory using cache-relative paths, including partial downloads and preserved ZIPs |
| `diagnostics_incomplete` | A secondary filesystem/logging failure prevented complete diagnostics; the original exception is retained |

Stages include source acquisition, cache verification, recovery checks, credential access, submission, upload, polling, ZIP download/extraction/validation, cache finalization, project indexing, original-PDF resolution and failed-attempt archival. Each stage start and completed transition is recorded, with an error event for a failed stage. Abrupt process termination can leave a started run with no final event; absence of an event is not proof that a remote request was never accepted.

## Redaction and limits

`diagnostics.py` uses a top-level field whitelist. Nested response objects, request headers, payloads, stdout/stderr, exception bodies and full API responses are not serialized. Only selected remote error fields enter diagnostics. Known in-memory Token values and their URL-encoded form are removed; whole HTTP(S) URLs are removed, including paths and signed query strings. Bearer/Basic values and labeled token/password/secret/signature/API-key/Authorization values are redacted. Control characters are removed and text fields are capped at 1,024 characters. Never add raw response logging to troubleshoot a failure.

Logs retain cache/batch IDs, relative filenames and process IDs. The manifest already contains local source paths or query-stripped source provenance for cache operation; it is not a public artifact. Review files before sharing. The actual Token is used only in memory for authorized API calls, never stored in a diagnostic field. DPAPI helper output is never logged, even on error. No expiry probing is introduced.

If the log directory cannot be created at all, the CLI may have only a controlled error and no usable log. If output inventory or manifest writing fails during another exception, preserve that original error and mark diagnostics incomplete when possible. Filesystem failure cannot guarantee durable records.

## Next steps by category

| Category | Next action |
|---|---|
| `LOCAL_TOKEN_MISSING` | Configure a Token in the local environment or supported local credential store; no MinerU request was made at this stage |
| `LOCAL_CREDENTIAL_ACCESS`, `LOCAL_CREDENTIAL_DECRYPTION` | Check helper availability, file permissions and the approved execution/user context. This is local access failure, not evidence of Token expiry. Do not repeatedly rotate credentials or automatically escalate privileges |
| `API_AUTH_REJECTED` | An actual API rejected authentication (401/403 or A0202/A0211). Replace or check credentials/permissions; existing completed caches remain usable |
| `API_RATE_LIMITED`, `API_SERVER_ERROR` | Record the observed HTTP status. If failure occurred during polling, resume the saved batch later; if during submission, preserve uncertainty and reconcile acceptance before any new POST |
| `API_TRANSPORT`, `API_HTTP_ERROR`, `API_REJECTED`, `API_RESPONSE_INVALID` | Use stage, status and envelope code to diagnose. Network/malformed/5xx/429 errors do not establish that a POST was unaccepted |
| `UPLOAD_FAILED`, `UPLOAD_RECONCILIATION_REQUIRED` | Keep the batch, use `--recover` to query it, and reconcile waiting-file manually. Storage-URL 401/403 is not automatically a MinerU Token rejection |
| `TASK_FAILED`, `TASK_PREVIOUSLY_FAILED` | Inspect remote_error and original log; archive the failed attempt before a deliberate, explicitly linked new attempt |
| `ZIP_DOWNLOAD_FAILED` | Keep the batch and partial download, repeat the source command to query/download the same task; no new POST or PUT |
| `ZIP_EXTRACT_FAILED`, `ZIP_VALIDATION_FAILED` | Keep result.zip. Inspect it; for a deliberate new download of the same task, use `--redownload-result`. The old ZIP is moved to `artifacts/`, never overwritten |
| `RESULT_CONFLICT` | Existing extracted output needs inspection before replacement; stop rather than silently overwrite |
| `CACHE_INTEGRITY`, `ORPHAN_CACHE`, `CACHE_STATE_INVALID` | Preserve evidence. A nonempty cache directory without manifest, or mismatched/unknown state, cannot initiate a new submission |

`CryptographicException` alone does not establish the exact DPAPI root cause. Likewise, a remote message such as “parsing failed, please try again later” and a later successful retry do not establish a transient service fault as the internal root cause.

## Recovery semantics

- `prepared` without a batch: can submit after credentials become available. An explicit authentication rejection during POST returns to prepared.
- `submission_unknown` without a batch: never automatically resubmit, even with `--recover`; reconcile via the service/account before further action.
- `upload_unknown`, `waiting-file`, or `submission_unknown` with a batch: `--recover` only queries the existing batch. It never repeats PUT or POST.
- A remote waiting-file result is saved as local waiting-file and returns a reconciliation error. A running/pending/converting result moves the local state to submitted and can be polled again.
- `submitted` requires a batch ID; a missing ID blocks submission. Completed caches and `--original` work offline without credential access.
- Confirmed failed tasks do not retry automatically.

Archive a failed attempt offline, keeping all its files and original manifest:

```sh
python <skill>/scripts/reader.py --archive-failed <cache_id> --cache-root <cache-root>
```

Use the returned `retry_from` directory name to deliberately start the next attempt with the same input and parameters:

```sh
python <skill>/scripts/reader.py <input.pdf> --language ch --cache-root <cache-root> --retry-from <archive-name>
```

The old and new attempts are linked in the new manifest and events. A manual archive such as `failed/ch4-attempt1` is also supported; its manifest must confirm failed and match the PDF/configuration. No new task is submitted if an archived failure exists but no explicit retry link is supplied. Archives with missing/corrupt manifests require manual reconciliation.

For interrupted result download, partial data stays in `downloads/<run_id>.zip`. A later run downloads to a different name. A fully downloaded but invalid result.zip is reused and fails again until inspected or explicitly redownloaded. `--redownload-result` preserves the previous ZIP first and does not resubmit the document. Existing mineru_raw is not silently replaced.

## Offline tests

```sh
python -m unittest discover -s skill/scripts -p "test_*.py"
```

Diagnostic tests mock HTTP responses and block accidental real network access. They cover errors, redaction, uncertain submissions, linked retries, retained failed ZIPs and offline cache reuse. They do not claim live service stability or scientific parsing accuracy.
