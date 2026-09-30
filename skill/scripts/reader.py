"""MinerU precision-only PDF ingestion. Python 3.10+, requests."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
import zipfile
import requests

API = 'https://mineru.net/api/v4'
class TokenRejected(RuntimeError):
    pass

TOKEN_HELP = ('MINERU_TOKEN_REJECTED: token expired, invalid, or authorization denied. '
              'Replace it locally using scripts/configure-token.ps1; never paste it into chat. '
              'Cached papers remain available. This is an API response, not a scheduled expiry check.')
CONFIG = Path(__file__).resolve().parents[1] / 'config.json'
DEFAULT_ROOT = (json.loads(CONFIG.read_text(encoding='utf-8'))['cache_root'] if CONFIG.exists()
                else str(Path.home() / 'Documents' / 'Codex' / 'MinerU-Library'))

def get_token():
    token = os.environ.get('MINERU_API_TOKEN', '').strip()
    if token or os.name != 'nt':
        return token
    credential = Path(os.environ.get('LOCALAPPDATA', '')) / 'CodexMinerU' / 'token.dpapi'
    if not credential.is_file():
        return ''
    command = "$s = Get-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'CodexMinerU/token.dpapi') | ConvertTo-SecureString; $p = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($p) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($p) }"
    result = subprocess.run([shutil.which('pwsh') or 'powershell', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Could not decrypt local MinerU credential')
    return result.stdout.strip()

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
        r.raise_for_status()
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
    r = requests.request(method, API + path, headers={'Authorization': 'Bearer ' + token},
                         json=payload, timeout=(20, 90), allow_redirects=False)
    if r.status_code in (401, 403):
        raise TokenRejected(TOKEN_HELP)
    if r.status_code != 200:
        raise RuntimeError('MinerU HTTP status ' + str(r.status_code))
    obj = r.json()
    if str(obj.get('code')) in ('A0202', 'A0211'):
        raise TokenRejected(TOKEN_HELP)
    if obj.get('code') != 0:
        raise RuntimeError('MinerU API code ' + str(obj.get('code')))
    return obj['data']

def extract(archive, target):
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        if sum(i.file_size for i in z.infolist()) > 2*1024**3:
            raise RuntimeError('Expanded archive exceeds 2 GiB')
        for info in z.infolist():
            name = info.filename.replace('\\', '/')
            parts = name.split('/')
            if name.startswith('/') or '..' in parts or any(':' in p for p in parts) or (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise RuntimeError('Unsafe ZIP entry')
            if info.is_dir():
                continue
            dest = target.joinpath(*parts)
            if not dest.resolve().is_relative_to(target.resolve()):
                raise RuntimeError('ZIP destination escapes extraction directory')
            dest.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, dest.open('wb') as out:
                shutil.copyfileobj(src, out)
    md = sorted(target.rglob('full.md'))
    js = sorted(target.rglob('*.json'))
    if not md or not any(p.stat().st_size for p in md) or not js:
        raise RuntimeError('Incomplete result: nonempty Markdown and JSON required')
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

def ingest(args):
    root = Path(args.cache_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=root, prefix='ingest-') as tmp:
        source = Path(tmp) / 'source.pdf'
        provenance = args.source
        if args.source.startswith(('https://', 'http://')):
            provenance = download(args.source, source)
        else:
            shutil.copyfile(Path(args.source), source)
        with source.open('rb') as f:
            if b'%PDF-' not in f.read(1024):
                raise RuntimeError('Not a PDF (possibly a login or abstract page)')
        if source.stat().st_size > 200*1024*1024:
            raise RuntimeError('PDF exceeds 200 MiB')
        params = {'model_version': 'vlm', 'enable_formula': True, 'enable_table': True,
                  'language': args.language, 'is_ocr': args.ocr}
        sha = digest(source)
        key = sha + '-' + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        folder = root / 'documents' / key
        with lock(root / 'locks' / (key + '.lock')):
            folder.mkdir(parents=True, exist_ok=True)
            manifest = folder / 'manifest.json'
            if manifest.exists():
                m = read(manifest)
            else:
                shutil.copyfile(source, folder / 'source.pdf')
                m = {'schema': 1, 'cache_id': key, 'sha256': sha, 'parameters': params,
                     'source': provenance, 'created_at': time.time(), 'state': 'prepared'}
                save(manifest, m)
            if m['state'] == 'done':
                if not all((folder / p).is_file() and digest(folder / p) == h for p, h in m['files'].items()):
                    raise RuntimeError('CACHE_DAMAGED: preserve evidence; repair from original result ZIP')
            else:
                if digest(folder / 'source.pdf') != m['sha256']:
                    raise RuntimeError('CACHE_DAMAGED: original PDF hash mismatch')
                token = get_token()
                if not token:
                    raise RuntimeError('MINERU_API_TOKEN is not configured; source preserved, no upload made')
                if m['state'] in ('submission_unknown', 'upload_unknown', 'waiting-file') and not (args.recover and 'batch_id' in m):
                    raise RuntimeError('AMBIGUOUS_SUBMISSION: inspect saved batch before retry; no automatic duplicate upload')
                if m['state'] == 'failed':
                    raise RuntimeError('Previous MinerU task failed; inspect manifest before explicit new attempt')
                if 'batch_id' not in m:
                    m['state'] = 'submission_unknown'
                    save(manifest, m)
                    payload = {k: v for k, v in params.items() if k != 'is_ocr'}
                    payload['files'] = [{'name': 'source.pdf', 'data_id': key, 'is_ocr': args.ocr}]
                    try:
                        data = api('POST', '/file-urls/batch', token, payload)
                    except TokenRejected:
                        # Explicit rejection means no accepted submission; allow retry with replacement token.
                        m['state'] = 'prepared'
                        save(manifest, m)
                        raise
                    m.update(batch_id=data['batch_id'], state='upload_unknown')
                    save(manifest, m)
                    if len(data.get('file_urls', [])) != 1:
                        raise RuntimeError('Invalid upload URL count; batch preserved for reconciliation')
                    with (folder / 'source.pdf').open('rb') as f:
                        r = requests.put(safe_url(data['file_urls'][0]), data=f, timeout=(20, 180), allow_redirects=False)
                        if r.status_code not in (200, 201, 204):
                            raise RuntimeError('Upload failed, HTTP ' + str(r.status_code))
                    m['state'] = 'submitted'
                    save(manifest, m)
                end = time.monotonic() + args.wait
                while True:
                    data = api('GET', '/extract-results/batch/' + m['batch_id'], token)
                    rows = data['extract_result']
                    row = next((r for r in rows if r.get('data_id') == key), rows[0] if len(rows) == 1 else None)
                    if row is None:
                        raise RuntimeError('No matching task result')
                    state = row['state']
                    m['remote_state'] = state
                    save(manifest, m)
                    if state == 'failed':
                        m['state'] = 'failed'
                        save(manifest, m)
                        raise RuntimeError('MinerU task failed; batch_id saved for diagnosis')
                    if state == 'done':
                        archive = folder / 'result.zip'
                        download(row['full_zip_url'], archive, 1024**3)
                        with tempfile.TemporaryDirectory(dir=folder, prefix='unpack-') as stage:
                            staged = Path(stage) / 'mineru_raw'
                            md, js = extract(archive, staged)
                            if (folder / 'mineru_raw').exists():
                                raise RuntimeError('Existing raw output requires inspection before replacement')
                            staged.replace(folder / 'mineru_raw')
                        m.update(state='done', completed_at=time.time(), markdown=md, structured_json=js,
                                 page_anchors='UNVERIFIED', semantic_quality='NOT_ASSESSED')
                        m['files'] = {str(p.relative_to(folder)): digest(p) for p in folder.rglob('*') if p.is_file() and p.name not in ('manifest.json', 'manifest.tmp')}
                        save(manifest, m)
                        break
                    if time.monotonic() >= end:
                        return {'state': 'pending', 'cache_id': key, 'batch_id': m['batch_id'], 'resume': 'Repeat the same command'}
                    time.sleep(min(5, max(0, end-time.monotonic())))
            link_project(args.project, key, args.title)
            return {'state': 'done', 'cache_id': key, 'directory': str(folder),
                    'markdown': [str(folder / p) for p in m['markdown']],
                    'structured_json': [str(folder / p) for p in m['structured_json']],
                    'page_anchors': 'UNVERIFIED', 'semantic_quality': 'NOT_ASSESSED'}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', help='Local PDF or direct HTTPS PDF URL')
    p.add_argument('--project', help='Project root; defaults to no project index')
    p.add_argument('--title', default='')
    p.add_argument('--language', default='en')
    p.add_argument('--ocr', action='store_true')
    p.add_argument('--recover', action='store_true', help='Query an existing uncertain batch without re-uploading')
    p.add_argument('--wait', type=int, default=45)
    p.add_argument('--cache-root', default=os.environ.get('MINERU_CACHE_ROOT', str(DEFAULT_ROOT)))
    args = p.parse_args()
    try:
        print(json.dumps(ingest(args), ensure_ascii=False, indent=2))
    except Exception as e:
        # Requests exceptions may contain signed URLs; never print their text.
        msg = str(e) if isinstance(e, RuntimeError) else type(e).__name__ + ': operation failed; credentials/URLs suppressed'
        print(json.dumps({'state': 'error', 'message': msg}, ensure_ascii=False))
        sys.exit(1)

if __name__ == '__main__':
    main()

