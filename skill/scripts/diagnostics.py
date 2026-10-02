"""Allowlisted diagnostics; never persist request/response objects or raw exceptions."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
import functools
import json
import os
import re
import time
import uuid
from urllib.parse import quote

CURRENT = ContextVar('mineru_run', default=None)
FIELDS = {'schema', 'time', 'run_id', 'event', 'stage', 'elapsed_ms', 'stage_elapsed_ms',
          'cache_id', 'batch_id', 'attempt_id', 'state', 'remote_state', 'previous_state',
          'recover', 'status', 'category', 'message', 'http_status', 'api_code',
          'err_code', 'err_msg', 'data_id', 'file_name', 'exception_type',
          'credential_source', 'credential_exists', 'process_returncode', 'dpapi_error_type',
          'retry_from', 'previous_batch_id', 'previous_manifest_sha256', 'file', 'bytes',
          'sha256', 'log', 'mode', 'output_count', 'request_sent', 'pid', 'diagnostics_incomplete'}

class DiagnosticError(RuntimeError):
    def __init__(self, category, message, **details):
        super().__init__(message)
        self.category = category
        self.details = details


def scrub(value, secrets=()):
    if not isinstance(value, (str, int, float, bool)) and value is not None:
        return '[omitted]'
    if not isinstance(value, str):
        return value
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            value = value.replace(secret, '[REDACTED]')
    # Strip whole URLs, not just queries; signed credentials can be in paths.
    value = re.sub(r'(?i)https?://[^\s<>\"\']+', '[REDACTED_URL]', value)
    value = re.sub(r'(?i)\b(?:bearer|basic)\s+[a-z0-9._~+/=\-]+', '[REDACTED_AUTH]', value)
    value = re.sub(r'(?i)(?:authorization|token|api[_-]?key|password|secret|signature)\s*[\"\']?\s*[:=]\s*(?:\"[^\"]*\"|\'[^\']*\'|[^\s,;}]+)', '[REDACTED_FIELD]', value)
    value = re.sub(r'[\x00-\x1f\x7f]', ' ', value)
    return value[:1024]


def clean(fields, secrets=()):
    return {k: scrub(v, secrets) for k, v in fields.items() if k in FIELDS}


def event(name, **fields):
    run = CURRENT.get()
    if run:
        run.emit(name, **fields)


def remember_secret(secret):
    run = CURRENT.get()
    if run and secret:
        run.secrets.update((secret, quote(secret, safe='')))


class Run:
    def __init__(self, root, mode, recover=False):
        self.root = Path(root).resolve()
        self.run_id = uuid.uuid4().hex
        self.path = self.root / 'logs' / 'unassigned' / (self.run_id + '.jsonl')
        self.started = self.stage_started = time.monotonic()
        self.phase = 'start'
        self.mode, self.recover = mode, recover
        self.secrets = set()
        self.m = self.folder = None
        self.ids = {}
        self.failure = None
        self.http_status = None

    def emit(self, name, **fields):
        if name == 'http_response':
            self.http_status = fields.get('http_status')
        now = time.monotonic()
        values = dict(schema=1, time=datetime.now(timezone.utc).isoformat(),
                      run_id=self.run_id, event=name, stage=self.phase,
                      elapsed_ms=round((now-self.started)*1000),
                      stage_elapsed_ms=round((now-self.stage_started)*1000),
                      recover=self.recover, mode=self.mode, pid=os.getpid(), **self.ids)
        if self.m is not None:
            values.update({k:self.m.get(k) for k in ('cache_id','batch_id','attempt_id','state','remote_state')})
        values.update(fields)
        safe = clean(values, self.secrets)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(safe, ensure_ascii=False) + '\n')
            f.flush()
        return safe

    def stage(self, name):
        self.emit('stage_finished', status='completed')
        self.phase, self.stage_started = name, time.monotonic()
        self.http_status = None
        self.emit('stage_started')

    def fail(self, exc):
        if isinstance(exc, DiagnosticError):
            category, message, details = exc.category, str(exc), exc.details
        else:
            categories = {'credential':'LOCAL_CREDENTIAL_ACCESS', 'submit':'API_TRANSPORT',
                          'poll':'API_TRANSPORT', 'upload':'UPLOAD_FAILED',
                          'zip_download':'ZIP_DOWNLOAD_FAILED', 'zip_extract':'ZIP_EXTRACT_FAILED',
                          'zip_validate':'ZIP_VALIDATION_FAILED', 'cache_verify':'CACHE_INTEGRITY',
                          'source':'SOURCE_FAILED', 'original':'ORIGINAL_RECOVERY_FAILED'}
            category = categories.get(self.phase, 'LOCAL_OPERATION_FAILED')
            message, details = 'Operation failed; raw exception text suppressed', {}
        details = dict(details)
        details.setdefault('http_status', self.http_status)
        try:
            self.failure = self.emit('error', category=category, message=message,
                                     exception_type=type(exc).__name__, **details)
        except OSError:
            self.failure = clean(dict(category=category, message=message, stage=self.phase,
                                      diagnostics_incomplete=True, **details), self.secrets)
        exc.safe_details = self.failure
        exc.diagnostic_log = str(self.path)
        return self.failure

    def identify(self, key):
        self.ids['cache_id'] = key
        destination = self.root / 'logs' / key / self.path.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.path != destination:
            self.path.replace(destination)
            self.path = destination

    @contextmanager
    def bound(self, folder, manifest, save):
        self.folder, self.m = folder, manifest
        self.ids = {k:manifest.get(k) for k in ('cache_id','batch_id','attempt_id')}
        self.identify(manifest['cache_id'])
        manifest['diagnostic_log'] = self.path.relative_to(self.root).as_posix()
        manifest.setdefault('diagnostic_logs', []).append(manifest['diagnostic_log'])
        primary = None
        try:
            self.emit('cache_bound')
            yield
        except Exception as exc:
            primary = exc
            manifest['last_error'] = self.fail(exc)
            raise
        finally:
            try:
                outputs = []
                for path in sorted(folder.rglob('*')):
                    if path.is_file() and path.name not in ('manifest.json', 'manifest.tmp'):
                        item = clean({'file':path.relative_to(folder).as_posix(), 'bytes':path.stat().st_size}, self.secrets)
                        outputs.append(item)
                        self.emit('output_file', **item)
                manifest['output_files'] = outputs
                self.emit('output_inventory', output_count=len(outputs))
                manifest['last_run'] = {'run_id':self.run_id, 'log':manifest['diagnostic_log'],
                                        'status':'error' if self.failure else 'returned',
                                        'elapsed_ms':round((time.monotonic()-self.started)*1000)}
                save(folder / 'manifest.json', manifest)
            except OSError:
                if primary is None:
                    raise DiagnosticError('DIAGNOSTIC_WRITE_FAILED', 'Could not finish local diagnostics; inspect cache before continuing') from None
                primary.safe_details['diagnostics_incomplete'] = True
                try:
                    save(folder / 'manifest.json', manifest)
                except OSError:
                    pass  # Preserve the primary error even if the filesystem is unavailable.
            finally:
                self.ids = {k:manifest.get(k) for k in ('cache_id','batch_id','attempt_id')}
                self.m = None


def logged(mode):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            root = args[0].cache_root if mode == 'ingest' else args[0]
            recover = bool(getattr(args[0], 'recover', False)) if mode == 'ingest' else False
            run = Run(root, mode, recover)
            marker = CURRENT.set(run)
            try:
                run.emit('run_started')
                result = fn(*args, **kwargs)
                run.emit('stage_finished', status='completed')
                run.emit('run_finished', status=result.get('state'))
                result['diagnostic_log'] = str(run.path)
                return result
            except Exception as exc:
                if run.failure is None:
                    run.fail(exc)
                raise
            finally:
                CURRENT.reset(marker)
                run.secrets.clear()
        return wrapped
    return decorate
