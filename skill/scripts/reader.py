"""MinerU precision-only PDF ingestion. Python 3.10+, requests."""
import argparse
import contextlib
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
import zipfile
import requests
import diagnostics as diag
from diagnostics import DiagnosticError

API = 'https://mineru.net/api/v4'
class TokenRejected(DiagnosticError):
    def __init__(self, message, **details):
        super().__init__('API_AUTH_REJECTED', message, **details)

TOKEN_HELP = ('MINERU_TOKEN_REJECTED: token expired, invalid, or authorization denied. '
              'Replace it locally using scripts/configure-token.ps1; never paste it into chat. '
              'Cached papers remain available. This is an API response, not a scheduled expiry check.')
CONFIG = Path(__file__).resolve().parents[1] / 'config.json'
DEFAULT_ROOT = (json.loads(CONFIG.read_text(encoding='utf-8'))['cache_root'] if CONFIG.exists()
                else str(Path.home() / 'Documents' / 'Codex' / 'MinerU-Library'))

def get_token():
    token = os.environ.get('MINERU_API_TOKEN', '').strip()
    if token:
        diag.remember_secret(token)
        diag.event('credential_available', credential_source='environment')
        return token
    if os.name != 'nt':
        return ''
    credential = Path(os.environ.get('LOCALAPPDATA', '')) / 'CodexMinerU' / 'token.dpapi'
    exists = credential.is_file()
    diag.event('credential_check', credential_source='dpapi', credential_exists=exists)
    if not exists:
        return ''
    command = "$ErrorActionPreference='Stop'; $s = Get-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'CodexMinerU/token.dpapi') | ConvertTo-SecureString; $p = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p) }"
    try:
        result = subprocess.run([shutil.which('pwsh') or 'powershell', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        raise DiagnosticError('LOCAL_CREDENTIAL_ACCESS', 'Local credential helper could not execute; API not called', credential_source='dpapi', request_sent=False) from None
    if result.returncode or not result.stdout.strip():
        error_type = 'CryptographicException' if 'CryptographicException' in (result.stderr or '') else 'unspecified'
        raise DiagnosticError('LOCAL_CREDENTIAL_DECRYPTION', 'Could not decrypt local MinerU credential; check execution context, not API token expiry', credential_source='dpapi', process_returncode=result.returncode, dpapi_error_type=error_type, request_sent=False)
    token = result.stdout.strip()
    diag.remember_secret(token)
    diag.event('credential_available', credential_source='dpapi')
    return token


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)

def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

@contextlib.contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError('BUSY: lock exists; check running process before removing stale lock')
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)

def safe_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not p.hostname or p.username or p.password:
        raise RuntimeError('Only HTTPS URLs without embedded credentials are supported')
    return url

def public_url(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, '', ''))

def download(url, dest, limit=200*1024*1024):
    safe_url(url)
    # No MinerU token is sent to source or signed storage URLs.
    with requests.get(url, stream=True, timeout=(20, 90)) as r:
        diag.event('http_response', http_status=r.status_code)
        if r.status_code != 200:
            run = diag.CURRENT.get()
            category = 'ZIP_DOWNLOAD_FAILED' if run and run.phase == 'zip_download' else 'SOURCE_DOWNLOAD_FAILED'
            raise DiagnosticError(category, 'Download HTTP error', http_status=r.status_code)
        safe_url(r.url)
        count = 0
        with dest.open('wb') as f:
            for block in r.iter_content(1024*1024):
                count += len(block)
                if count > limit:
                    raise RuntimeError('Download exceeds size limit')
                f.write(block)
        return public_url(r.url)

def api(method, path, token, payload=None):
    diag.remember_secret(token)
    r = requests.request(method, API + path, headers={'Authorization': 'Bearer ' + token},
                         json=payload, timeout=(20, 90), allow_redirects=False)
    diag.event('http_response', http_status=r.status_code)
    if r.status_code in (401, 403):
        raise TokenRejected(TOKEN_HELP, http_status=r.status_code)
    if r.status_code != 200:
        category = 'API_RATE_LIMITED' if r.status_code == 429 else 'API_SERVER_ERROR' if r.status_code >= 500 else 'API_HTTP_ERROR'
        raise DiagnosticError(category, 'MinerU HTTP status ' + str(r.status_code), http_status=r.status_code)
    try:
        obj = r.json()
    except ValueError:
        raise DiagnosticError('API_RESPONSE_INVALID', 'MinerU response is not valid JSON', http_status=r.status_code) from None
    if not isinstance(obj, dict):
        raise DiagnosticError('API_RESPONSE_INVALID', 'MinerU response is not an object', http_status=r.status_code)
    code = obj.get('code')
    if str(code) in ('A0202', 'A0211'):
        raise TokenRejected(TOKEN_HELP, http_status=r.status_code, api_code=code)
    if code != 0:
        raise DiagnosticError('API_REJECTED', 'MinerU API returned an error code', http_status=r.status_code, api_code=code, err_msg=obj.get('msg'))
    if not isinstance(obj.get('data'), dict):
        raise DiagnosticError('API_RESPONSE_INVALID', 'MinerU response has no data object', http_status=r.status_code)
    return obj['data']


def extract(archive, target):
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        if sum(i.file_size for i in z.infolist()) > 2*1024**3:
            raise DiagnosticError('ZIP_EXTRACT_FAILED', 'Expanded archive exceeds 2 GiB')
        for info in z.infolist():
            name = info.filename.replace('\\', '/')
            parts = name.split('/')
            if name.startswith('/') or '..' in parts or any(':' in p for p in parts) or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise DiagnosticError('ZIP_EXTRACT_FAILED', 'Unsafe ZIP entry')
            if info.is_dir():
                continue
            dest = target.joinpath(*parts)
            if not dest.resolve().is_relative_to(target.resolve()):
                raise DiagnosticError('ZIP_EXTRACT_FAILED', 'ZIP destination escapes extraction directory')
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, dest.open('wb') as out:
                shutil.copyfileobj(src, out)
    run = diag.CURRENT.get()
    if run:
        run.stage('zip_validate')
    md = sorted(target.rglob('full.md'))
    js = sorted(target.rglob('*.json'))
    if not md or not any(p.stat().st_size for p in md) or not js:
        raise DiagnosticError('ZIP_VALIDATION_FAILED', 'Incomplete result: nonempty Markdown and JSON required')
    for p in js:
        read(p)
    return [str(p.relative_to(target.parent)) for p in md], [str(p.relative_to(target.parent)) for p in js]

def link_project(project, key, title):
    if not project:
        return
    folder = Path(project).resolve() / 'literature'
    with lock(folder / '.index.lock'):
        p = folder / 'index.json'
        obj = read(p) if p.exists() else {'schema': 1, 'documents': {}}
        obj['documents'].setdefault(key, {'title': title, 'cache_id': key})
        save(p, obj)

def cache_path(folder, relative):
    """Resolve a manifest path without allowing writes/deletes outside its cache."""
    path = folder / relative
    if not path.resolve().is_relative_to(folder.resolve()) or path.is_symlink():
        raise RuntimeError('Unsafe cache path')
    return path

def same_original(path, sha):
    try:
        return path.is_file() and digest(path) == sha
    except OSError:
        return False

def plan_original_storage(folder, m):
    """Plan cleanup only for new caches, retaining a byte-identical ZIP backup."""
    sha = m['sha256']
    original = {'kind': m['source_kind'], 'local_path': m.get('local_source'),
                'cached_path': 'source.pdf', 'zip_member': None}
    m['original'] = original
    matches = []
    with zipfile.ZipFile(folder / 'result.zip') as z:
        for info in z.infolist():
            if not info.is_dir() and info.filename.lower().endswith('.pdf'):
                with z.open(info) as stream:
                    h = hashlib.sha256()
                    for block in iter(lambda: stream.read(1024 * 1024), b''):
                        h.update(block)
                relative = 'mineru_raw/' + info.filename.replace('\\', '/')
                if h.hexdigest() == sha and same_original(cache_path(folder, relative), sha):
                    matches.append((info.filename, relative))
    if not matches:
        # Missing/different original in ZIP: preserve the input snapshot.
        return
    original['zip_member'] = matches[0][0]
    external = Path(m['local_source']) if m.get('local_source') else None
    use_external = external is not None and same_original(external, sha)
    original['cached_path'] = None if use_external else matches[0][1]
    keep = external.resolve() if use_external else cache_path(folder, original['cached_path']).resolve()
    removals = []
    for relative in ['source.pdf'] + [r for _, r in matches]:
        path = cache_path(folder, relative)
        if path.resolve() != keep and same_original(path, sha):
            removals.append(relative)
            m['files'].pop(relative, None)
            # Existing manifests use the platform's path separator.
            m['files'].pop(str(Path(relative)), None)
    m['original_cleanup_pending'] = sorted(set(removals))

def finish_original_cleanup(folder, m):
    """Manifest is saved first, so interrupted cleanup can safely be resumed."""
    pending = m.get('original_cleanup_pending', [])
    if not pending:
        return
    if digest(folder / 'result.zip') != m['files']['result.zip']:
        raise RuntimeError('CACHE_DAMAGED: result ZIP hash mismatch; original retained')
    for relative in pending:
        path = cache_path(folder, relative)
        if path.exists():
            if not same_original(path, m['sha256']):
                raise RuntimeError('CACHE_DAMAGED: cleanup candidate changed; original retained')
            path.unlink()
    m.pop('original_cleanup_pending', None)
    save(folder / 'manifest.json', m)

def verify_cache(folder, m):
    if not all(same_original(cache_path(folder, p), h) for p, h in m['files'].items()):
        raise RuntimeError('CACHE_DAMAGED: preserve evidence; repair from original result ZIP')

def original_pdf(folder, m):
    """Resolve the parsed version, restoring from ZIP if the local input changed."""
    original = m.get('original', {'cached_path': 'source.pdf'})
    external = original.get('local_path')
    if external and same_original(Path(external), m['sha256']):
        return str(Path(external).resolve())
    relative = original.get('cached_path')
    if relative and same_original(cache_path(folder, relative), m['sha256']):
        return str(cache_path(folder, relative))
    member = original.get('zip_member')
    if not member:
        raise RuntimeError('CACHE_DAMAGED: no verified original PDF available')
    archive = folder / 'result.zip'
    if digest(archive) != m['files']['result.zip']:
        raise RuntimeError('CACHE_DAMAGED: result ZIP hash mismatch')
    # Restore outside mineru_raw; never overwrite the user's input or parsed files.
    dest = cache_path(folder, 'restored-original.pdf')
    if dest.exists() and not same_original(dest, m['sha256']):
        raise RuntimeError('CACHE_DAMAGED: recovery destination already contains different data')
    if not dest.exists():
        with tempfile.TemporaryDirectory(dir=folder, prefix='restore-') as tmp:
            staged = Path(tmp) / 'original.pdf'
            with zipfile.ZipFile(archive) as z, z.open(member) as src, staged.open('wb') as out:
                shutil.copyfileobj(src, out)
            if not same_original(staged, m['sha256']):
                raise RuntimeError('CACHE_DAMAGED: ZIP original PDF hash mismatch')
            staged.replace(dest)
    original['cached_path'] = dest.name
    m['files'][dest.name] = m['sha256']
    save(folder / 'manifest.json', m)
    return str(dest)

@diag.logged('original')
def resolve_original(root, key):
    if not re.fullmatch(r'[0-9a-f]{64}-[0-9a-f]{16}', key):
        raise RuntimeError('Invalid cache ID')
    root = Path(root).resolve()
    folder = root / 'documents' / key
    if not (folder / 'manifest.json').is_file():
        raise RuntimeError('Cache ID not found')
    with lock(root / 'locks' / (key + '.lock')):
        m = read(folder / 'manifest.json')
        if m.get('cache_id') != key:
            raise DiagnosticError('CACHE_STATE_INVALID', 'Manifest cache ID does not match requested cache')
        run = diag.CURRENT.get()
        with run.bound(folder, m, save):
            run.stage('cache_verify')
            if m['state'] != 'done':
                raise RuntimeError('Parsing is not complete; original cleanup is not available')
            verify_cache(folder, m)
            finish_original_cleanup(folder, m)
            return {'state': 'done', 'cache_id': key, 'original_pdf': original_pdf(folder, m)}

def change_state(folder, m, state=None, remote=None):
    previous = m.get('state')
    if state is not None:
        m['state'] = state
    if remote is not None:
        m['remote_state'] = remote
    save(folder / 'manifest.json', m)
    diag.event('state_changed', previous_state=previous, state=m['state'], remote_state=m.get('remote_state'))


def valid_key(key):
    if not re.fullmatch(r'[0-9a-f]{64}-[0-9a-f]{16}', key):
        raise DiagnosticError('CACHE_ID_INVALID', 'Invalid cache ID')


@diag.logged('archive_failed')
def archive_failed(root, key):
    valid_key(key)
    root = Path(root).resolve()
    folder = cache_path(root, 'documents/' + key)
    run = diag.CURRENT.get()
    run.identify(key)
    run.stage('archive_failed')
    with lock(root / 'locks' / (key + '.lock')):
        m = read(folder / 'manifest.json')
        if m['state'] != 'failed':
            raise DiagnosticError('RETRY_BLOCKED', 'Only confirmed failed tasks can be archived for retry')
        name = key + '-attempt-' + run.run_id
        dest = cache_path(root, 'failed/' + name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        old_hash = digest(folder / 'manifest.json')
        # Both resolved paths are confined to this cache root. Rename preserves all evidence.
        folder.rename(dest)
        run.emit('failed_archived', batch_id=m.get('batch_id'), retry_from=name, previous_manifest_sha256=old_hash)
        return {'state':'archived', 'cache_id':key, 'retry_from':name,
                'previous_batch_id':m.get('batch_id'), 'previous_manifest_sha256':old_hash}


def retry_link(root, name, key, params):
    if not name or Path(name).name != name or not re.fullmatch(r'[a-zA-Z0-9._-]+', name):
        raise DiagnosticError('RETRY_BLOCKED', 'Invalid failed-attempt directory name')
    old_path = cache_path(root, 'failed/' + name) / 'manifest.json'
    old = read(old_path)
    if old.get('state') != 'failed' or old.get('cache_id') != key or old.get('parameters') != params:
        raise DiagnosticError('RETRY_BLOCKED', 'Archived failed attempt does not match this PDF and parameters')
    return {'archive': 'failed/' + name, 'batch_id':old.get('batch_id'),
            'attempt_id':old.get('attempt_id'), 'manifest_sha256':digest(old_path)}


def finish_result(folder, m, row, args, run):
    archive = folder / 'result.zip'
    run.stage('zip_download')
    if getattr(args, 'redownload_result', False) and archive.exists():
        if (folder / 'mineru_raw').exists():
            raise DiagnosticError('RESULT_CONFLICT', 'Extracted output exists; inspect it before redownloading')
        previous = cache_path(folder, 'artifacts/result-' + run.run_id + '.zip')
        previous.parent.mkdir(parents=True, exist_ok=True)
        archive.rename(previous)
        run.emit('result_preserved', file=previous.relative_to(folder).as_posix())
    if not archive.exists():
        candidate = cache_path(folder, 'downloads/' + run.run_id + '.zip')
        candidate.parent.mkdir(parents=True, exist_ok=True)
        download(row['full_zip_url'], candidate, 1024**3)
        candidate.replace(archive)
    else:
        run.emit('zip_reused', file='result.zip')
    run.stage('zip_extract')
    with tempfile.TemporaryDirectory(dir=folder, prefix='unpack-') as stage:
        staged = Path(stage) / 'mineru_raw'
        md, js = extract(archive, staged)
        if (folder / 'mineru_raw').exists():
            raise DiagnosticError('RESULT_CONFLICT', 'Existing raw output requires inspection before replacement')
        staged.replace(folder / 'mineru_raw')
    run.stage('cache_finalize')
    m.update(completed_at=time.time(), markdown=md, structured_json=js,
             page_anchors='UNVERIFIED', semantic_quality='NOT_ASSESSED')
    m['files'] = {p.relative_to(folder).as_posix():digest(p) for p in folder.rglob('*')
                  if p.is_file() and p.name not in ('manifest.json', 'manifest.tmp')}
    if 'source_kind' in m:
        plan_original_storage(folder, m)
    change_state(folder, m, 'done')
    finish_original_cleanup(folder, m)


@diag.logged('ingest')
def ingest(args):
    run = diag.CURRENT.get()
    root = Path(args.cache_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    run.stage('source')
    with tempfile.TemporaryDirectory(dir=root, prefix='ingest-') as tmp:
        source = Path(tmp) / 'source.pdf'
        provenance = args.source
        if args.source.startswith(('https://', 'http://')):
            provenance = download(args.source, source)
        else:
            shutil.copyfile(Path(args.source), source)
            provenance = str(Path(args.source).resolve())
        with source.open('rb') as f:
            if b'%PDF-' not in f.read(1024):
                raise RuntimeError('Not a PDF (possibly a login or abstract page)')
        if source.stat().st_size > 200*1024*1024:
            raise RuntimeError('PDF exceeds 200 MiB')
        params = {'model_version': 'vlm', 'enable_formula': True, 'enable_table': True,
                  'language': args.language, 'is_ocr': args.ocr}
        sha = digest(source)
        key = sha + '-' + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        run.identify(key)
        folder = root / 'documents' / key
        with lock(root / 'locks' / (key + '.lock')):
            folder.mkdir(parents=True, exist_ok=True)
            manifest = folder / 'manifest.json'
            retry_from = getattr(args, 'retry_from', None)
            if manifest.exists():
                m = read(manifest)
                if m.get('cache_id') != key:
                    raise DiagnosticError('CACHE_STATE_INVALID', 'Manifest cache ID does not match input')
                if retry_from and m.get('previous_attempt', {}).get('archive') != 'failed/' + retry_from:
                    raise DiagnosticError('RETRY_BLOCKED', 'Active cache belongs to a different attempt; preserve it first')
            else:
                if any(folder.iterdir()):
                    raise DiagnosticError('ORPHAN_CACHE', 'Cache directory has files but no manifest; preserve and inspect it before submitting')
                previous = retry_link(root, retry_from, key, params) if retry_from else None
                # A manually archived failure must also be linked before a new POST.
                if previous is None and (root / 'failed').exists():
                    for old in (root / 'failed').glob('*/manifest.json'):
                        old_manifest = read(old)
                        if old_manifest.get('cache_id') == key and old_manifest.get('state') == 'failed':
                            raise DiagnosticError('RETRY_LINK_REQUIRED', 'Archived failure exists; specify --retry-from to link the new attempt')
                shutil.copyfile(source, folder / 'source.pdf')
                m = {'schema':1, 'cache_id':key, 'sha256':sha, 'parameters':params,
                     'source':provenance, 'created_at':time.time(), 'state':'prepared', 'attempt_id':run.run_id}
                m['source_kind'] = 'network' if args.source.startswith(('https://', 'http://')) else 'local'
                if m['source_kind'] == 'local':
                    m['local_source'] = provenance
                if previous:
                    m['previous_attempt'] = previous
                save(manifest, m)
            with run.bound(folder, m, save):
                if m.get('previous_attempt'):
                    previous = m['previous_attempt']
                    run.emit('retry_linked', retry_from=previous['archive'], previous_batch_id=previous.get('batch_id'), previous_manifest_sha256=previous['manifest_sha256'])
                run.stage('cache_verify')
                if m['state'] == 'done':
                    verify_cache(folder, m)
                    finish_original_cleanup(folder, m)
                else:
                    if digest(folder / 'source.pdf') != m['sha256']:
                        raise RuntimeError('CACHE_DAMAGED: original PDF hash mismatch')
                    run.stage('recovery_check')
                    if m['state'] == 'failed':
                        raise DiagnosticError('TASK_PREVIOUSLY_FAILED', 'Previous MinerU task failed; archive it before an explicitly linked retry')
                    if m['state'] not in ('prepared','submission_unknown','upload_unknown','waiting-file','submitted'):
                        raise DiagnosticError('CACHE_STATE_INVALID', 'Unknown cache state; no task submitted')
                    if m['state'] != 'prepared' and not m.get('batch_id'):
                        raise DiagnosticError('AMBIGUOUS_SUBMISSION', 'Submission state has no batch ID; reconcile manually, no automatic POST')
                    if m['state'] in ('submission_unknown','upload_unknown','waiting-file') and not getattr(args, 'recover', False):
                        raise DiagnosticError('AMBIGUOUS_SUBMISSION', 'Use --recover to query the saved batch; no automatic duplicate upload')
                    run.stage('credential')
                    token = get_token()
                    if not token:
                        raise DiagnosticError('LOCAL_TOKEN_MISSING', 'MINERU_API_TOKEN is not configured; source preserved, no upload made', request_sent=False)
                    diag.remember_secret(token)
                    if not m.get('batch_id'):
                        run.stage('submit')
                        change_state(folder, m, 'submission_unknown')
                        payload = {k:v for k,v in params.items() if k != 'is_ocr'}
                        payload['files'] = [{'name':'source.pdf','data_id':key,'is_ocr':args.ocr}]
                        try:
                            data = api('POST', '/file-urls/batch', token, payload)
                        except TokenRejected:
                            change_state(folder, m, 'prepared')
                            raise
                        if not isinstance(data.get('batch_id'), str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', data['batch_id']):
                            raise DiagnosticError('API_RESPONSE_INVALID', 'Missing or invalid batch ID; submission acceptance unknown')
                        if any(secret in data['batch_id'] for secret in run.secrets):
                            raise DiagnosticError('API_RESPONSE_INVALID', 'Batch ID contains credential material; submission acceptance unknown')
                        m['batch_id'] = data['batch_id']
                        change_state(folder, m, 'upload_unknown')
                        run.stage('upload')
                        if len(data.get('file_urls', [])) != 1:
                            raise DiagnosticError('UPLOAD_FAILED', 'Invalid upload URL count; batch preserved for reconciliation', request_sent=False)
                        with (folder / 'source.pdf').open('rb') as f:
                            r = requests.put(safe_url(data['file_urls'][0]), data=f, timeout=(20, 180), allow_redirects=False)
                            run.emit('http_response', http_status=r.status_code)
                            if r.status_code not in (200,201,204):
                                raise DiagnosticError('UPLOAD_FAILED', 'Upload HTTP error; reconcile saved batch before retry', http_status=r.status_code)
                        change_state(folder, m, 'submitted')
                    else:
                        run.emit('batch_resumed', recover=getattr(args, 'recover', False))
                    end = time.monotonic() + args.wait
                    while True:
                        run.stage('poll')
                        data = api('GET', '/extract-results/batch/' + m['batch_id'], token)
                        rows = data.get('extract_result')
                        if not isinstance(rows, list) or not rows or any(not isinstance(r,dict) for r in rows):
                            raise DiagnosticError('API_RESPONSE_INVALID', 'Task result list missing or invalid')
                        row = next((r for r in rows if r.get('data_id') == key), None)
                        if row is None and len(rows)==1 and not rows[0].get('data_id'):
                            row = rows[0]
                        if row is None:
                            raise DiagnosticError('API_RESPONSE_INVALID', 'No matching task result')
                        state = row.get('state')
                        if state not in ('waiting-file','pending','running','converting','done','failed'):
                            raise DiagnosticError('API_RESPONSE_INVALID', 'Unknown remote task state')
                        change_state(folder, m, remote=state)
                        if state == 'failed':
                            details = {k:row.get(k) for k in ('err_code','err_msg','data_id','file_name')}
                            m['remote_error'] = diag.clean(details, run.secrets)
                            change_state(folder, m, 'failed')
                            raise DiagnosticError('TASK_FAILED', 'MinerU task failed; remote error saved, internal root cause unknown', **details)
                        if state == 'waiting-file':
                            change_state(folder, m, 'waiting-file')
                            raise DiagnosticError('UPLOAD_RECONCILIATION_REQUIRED', 'Batch is waiting for a file; reconcile upload manually, no automatic PUT')
                        if state == 'done':
                            finish_result(folder, m, row, args, run)
                            break
                        change_state(folder, m, 'submitted')
                        if time.monotonic() >= end:
                            run.emit('poll_wait_expired', status='pending')
                            return {'state':'pending','cache_id':key,'batch_id':m['batch_id'],'resume':'Repeat the same command'}
                        time.sleep(min(5,max(0,end-time.monotonic())))
                run.stage('project_index')
                link_project(args.project, key, args.title)
                run.stage('original')
                return {'state':'done','cache_id':key,'batch_id':m.get('batch_id'),'directory':str(folder),
                        'original_pdf':original_pdf(folder,m),
                        'markdown':[str(folder/p) for p in m['markdown']],
                        'structured_json':[str(folder/p) for p in m['structured_json']],
                        'page_anchors':'UNVERIFIED','semantic_quality':'NOT_ASSESSED'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', nargs='?', help='Local PDF or direct HTTPS PDF URL')
    p.add_argument('--original', metavar='CACHE_ID', help='Locate/recover the parsed original offline by cache ID')
    p.add_argument('--archive-failed', metavar='CACHE_ID', help='Archive a confirmed failed cache offline, preserving all evidence')
    p.add_argument('--retry-from', metavar='ARCHIVE_NAME', help='Explicitly link a new submission to an archived failed attempt')
    p.add_argument('--redownload-result', action='store_true', help='Preserve an old ZIP before downloading again; never resubmit the task')
    p.add_argument('--project', help='Project root; defaults to no project index')
    p.add_argument('--title', default='')
    p.add_argument('--language', default='en')
    p.add_argument('--ocr', action='store_true')
    p.add_argument('--recover', action='store_true', help='Query an existing uncertain batch without re-uploading')
    p.add_argument('--wait', type=int, default=45)
    p.add_argument('--cache-root', default=os.environ.get('MINERU_CACHE_ROOT', str(DEFAULT_ROOT)))
    args = p.parse_args()
    if sum(bool(v) for v in (args.source,args.original,args.archive_failed)) != 1:
        p.error('Provide one PDF source, --original CACHE_ID or --archive-failed CACHE_ID')
    if (args.retry_from or args.redownload_result) and not args.source:
        p.error('Retry options require a PDF source')
    try:
        result = archive_failed(args.cache_root,args.archive_failed) if args.archive_failed else resolve_original(args.cache_root,args.original) if args.original else ingest(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        details = getattr(e, 'safe_details', {'category':'LOCAL_OPERATION_FAILED','message':'Operation failed; raw exception text suppressed'})
        print(json.dumps({'state':'error', 'error':details, 'diagnostic_log':getattr(e,'diagnostic_log',None)}, ensure_ascii=False))
        sys.exit(1)

if __name__ == '__main__':
    main()

