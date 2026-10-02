"""Offline diagnostics and recovery contract tests. No live MinerU requests."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile
import reader
import diagnostics as diag

TOKEN = 'test-secret-never-persist-0192837465'
SIGNED = 'https://storage.example/private?signature=SIGNED-SECRET&token=QUERY-SECRET'

class Response:
    def __init__(self, status=200, data=None, content=None):
        self.status_code, self.data, self.content = status, data, content
        self.url = SIGNED
    def json(self):
        if isinstance(self.data, Exception): raise self.data
        return self.data
    def iter_content(self, n): yield self.content
    def __enter__(self): return self
    def __exit__(self, *args): pass

class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.pdf = self.base/'paper.pdf'; self.pdf.write_bytes(b'%PDF-1.4\nfixture')
        self.args = argparse.Namespace(source=str(self.pdf),cache_root=str(self.base/'cache'),
            project=None,title='',language='ch',ocr=False,wait=0,recover=False,
            retry_from=None,redownload_result=False)
        self.network_block = patch('requests.sessions.Session.send', side_effect=AssertionError('Live network forbidden'))
        self.network_block.start(); self.addCleanup(self.network_block.stop)

    def zip(self, invalid=False):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            z.writestr('full.md','# test' if not invalid else '')
            z.writestr('content_list.json','[]')
            z.writestr('origin.pdf',self.pdf.read_bytes())
        return b.getvalue()

    def request(self, responses, *, put=None, zip_response=None):
        from contextlib import ExitStack
        stack=ExitStack()
        stack.enter_context(patch.object(reader,'get_token',return_value=TOKEN))
        req=stack.enter_context(patch.object(reader.requests,'request',side_effect=responses))
        upload=stack.enter_context(patch.object(reader.requests,'put',side_effect=put if isinstance(put,Exception) else None,return_value=put or Response(200)))
        stack.enter_context(patch.object(reader.requests,'get',side_effect=zip_response if isinstance(zip_response,Exception) else None,return_value=zip_response or Response(content=self.zip())))
        self.addCleanup(stack.close)
        return req,upload

    def post(self): return Response(data={'code':0,'data':{'batch_id':'batch-1','file_urls':[SIGNED]}})
    def poll(self, state='done', **extra):
        row={'state':state, **extra}
        if state=='done': row['full_zip_url']=SIGNED
        return Response(data={'code':0,'data':{'extract_result':[row]}})
    def folder(self): return next((self.base/'cache/documents').iterdir())
    def manifest(self): return reader.read(self.folder()/'manifest.json')
    def events(self):
        return [json.loads(line) for p in (self.base/'cache/logs').rglob('*.jsonl') for line in p.read_text(encoding='utf-8').splitlines()]
    def assert_category(self, expected):
        self.assertEqual(self.manifest()['last_error']['category'],expected)
        self.assertTrue(any(e.get('category')==expected for e in self.events()))
        self.assertTrue((self.base/'cache'/self.manifest()['diagnostic_log']).is_file())
    def assert_clean(self):
        texts=[p.read_text(encoding='utf-8') for p in (self.base/'cache').rglob('*.jsonl')]
        texts += [p.read_text(encoding='utf-8') for p in (self.base/'cache').rglob('manifest.json')]
        text='\n'.join(texts)
        for secret in (TOKEN,SIGNED,'SIGNED-SECRET','QUERY-SECRET','UNEXPECTED-RESPONSE-SECRET'):
            self.assertNotIn(secret,text)
        for event in self.events(): self.assertLessEqual(set(event),diag.FIELDS)

    def test_missing_token_local_before_api(self):
        with patch.object(reader,'get_token',return_value=''),patch.object(reader,'api') as api:
            with self.assertRaises(diag.DiagnosticError): reader.ingest(self.args)
            api.assert_not_called()
        self.assert_category('LOCAL_TOKEN_MISSING')
        self.assertEqual(self.manifest()['state'],'prepared')
        self.assertFalse(self.manifest()['last_error']['request_sent'])

    def test_dpapi_failure_is_local_not_auth(self):
        with patch.dict(reader.os.environ,{'MINERU_API_TOKEN':'','LOCALAPPDATA':str(self.base)}):
            cred=self.base/'CodexMinerU/token.dpapi'; cred.parent.mkdir(); cred.write_text('encrypted-fixture')
            process=Mock(returncode=1,stdout=TOKEN,stderr='CryptographicException '+TOKEN+' '+SIGNED)
            with patch.object(reader.subprocess,'run',return_value=process),patch.object(reader,'api') as api:
                with self.assertRaises(diag.DiagnosticError): reader.ingest(self.args)
                api.assert_not_called()
        self.assert_category('LOCAL_CREDENTIAL_DECRYPTION')
        self.assertEqual(self.manifest()['last_error']['dpapi_error_type'],'CryptographicException')
        self.assert_clean()

    def test_dpapi_helper_timeout(self):
        with patch.dict(reader.os.environ,{'MINERU_API_TOKEN':'','LOCALAPPDATA':str(self.base)}):
            cred=self.base/'CodexMinerU/token.dpapi'; cred.parent.mkdir(); cred.write_text('encrypted')
            with patch.object(reader.subprocess,'run',side_effect=subprocess.TimeoutExpired('x',20,output=TOKEN)):
                with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assert_category('LOCAL_CREDENTIAL_ACCESS'); self.assert_clean()

    def test_post_auth_rejection_is_retryable_prepared(self):
        self.request([Response(401)])
        with self.assertRaises(reader.TokenRejected):reader.ingest(self.args)
        self.assert_category('API_AUTH_REJECTED')
        self.assertEqual(self.manifest()['state'],'prepared')
        self.assertEqual(self.manifest()['last_error']['http_status'],401)
        self.assert_clean()

    def test_api_business_auth_code(self):
        self.request([Response(data={'code':'A0211','msg':TOKEN})])
        with self.assertRaises(reader.TokenRejected):reader.ingest(self.args)
        self.assertEqual(self.manifest()['last_error']['api_code'],'A0211')
        self.assert_category('API_AUTH_REJECTED'); self.assert_clean()

    def test_poll_rate_limit_and_server_error_keep_batch(self):
        for status,category in [(429,'API_RATE_LIMITED'),(503,'API_SERVER_ERROR')]:
            req,_=self.request([self.post(),Response(status)] if not (self.base/'cache/documents').exists() else [Response(status)])
            with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
            self.assert_category(category)
            self.assertEqual(self.manifest()['state'],'submitted')
            self.assertEqual(self.manifest()['batch_id'],'batch-1')
            self.assertEqual(self.manifest()['last_error']['http_status'],status)

    def test_unknown_post_not_repeated_even_with_recover(self):
        req,upload=self.request([reader.requests.Timeout(TOKEN+' '+SIGNED)])
        with self.assertRaises(reader.requests.Timeout):reader.ingest(self.args)
        self.assert_category('API_TRANSPORT'); self.assertEqual(self.manifest()['state'],'submission_unknown')
        self.args.recover=True
        with self.assertRaisesRegex(diag.DiagnosticError,'no batch ID'):reader.ingest(self.args)
        self.assertEqual(req.call_count,1); upload.assert_not_called(); self.assert_clean()

    def test_upload_http_failure_not_token_rejection(self):
        self.request([self.post()],put=Response(403))
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assert_category('UPLOAD_FAILED'); self.assertEqual(self.manifest()['state'],'upload_unknown')
        self.assertEqual(self.manifest()['last_error']['http_status'],403)

    def test_upload_timeout_recover_only_queries(self):
        req,upload=self.request([self.post(),self.poll('running')],put=reader.requests.Timeout(SIGNED))
        with self.assertRaises(reader.requests.Timeout):reader.ingest(self.args)
        self.assert_category('UPLOAD_FAILED')
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.args.recover=True
        self.assertEqual(reader.ingest(self.args)['state'],'pending')
        self.assertEqual(upload.call_count,1)
        self.assertEqual([c.args[0] for c in req.call_args_list],['POST','GET'])
        self.assertEqual(self.manifest()['state'],'submitted'); self.assert_clean()

    def test_waiting_file_requires_manual_reconciliation(self):
        req,upload=self.request([self.post(),self.poll('waiting-file'),self.poll('waiting-file')])
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assert_category('UPLOAD_RECONCILIATION_REQUIRED')
        self.assertEqual(self.manifest()['state'],'waiting-file')
        self.args.recover=True
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assertEqual(upload.call_count,1)
        self.assertEqual([c.args[0] for c in req.call_args_list],['POST','GET','GET'])

    def test_remote_error_missing_code_and_redaction(self):
        text='parsing failed, please try again later; Bearer '+TOKEN+' '+SIGNED+' token=OTHER-SECRET'
        self.request([self.post(),self.poll('failed',err_msg=text,file_name='ch4.pdf',extra={'token':'UNEXPECTED-RESPONSE-SECRET'})])
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        m=self.manifest(); self.assertEqual(m['state'],'failed')
        self.assertIn('parsing failed, please try again later',m['remote_error']['err_msg'])
        self.assertIsNone(m['remote_error']['err_code'])
        self.assertNotIn('OTHER-SECRET',json.dumps(m))
        self.assertEqual(m['remote_error']['file_name'],'ch4.pdf')
        self.assert_category('TASK_FAILED'); self.assert_clean()

    def test_remote_error_with_code(self):
        self.request([self.post(),self.poll('failed',err_code='E_TEST',err_msg='parser failed')])
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assertEqual(self.manifest()['remote_error']['err_code'],'E_TEST')
        self.assertEqual(self.manifest()['last_error']['err_code'],'E_TEST')

    def test_failed_archive_linked_retry_preserves_old_manifest(self):
        req,upload=self.request([self.post(),self.poll('failed',err_msg='parsing failed, please try again later'),self.post(),self.poll()])
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        key=self.manifest()['cache_id']
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assertEqual(req.call_count,2)
        original=(self.folder()/'manifest.json').read_bytes()
        archived=reader.archive_failed(self.args.cache_root,key)
        old=self.base/'cache/failed'/archived['retry_from']/'manifest.json'
        self.assertEqual(old.read_bytes(),original)
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assertEqual(req.call_count,2)
        self.args.retry_from=archived['retry_from']
        result=reader.ingest(self.args)
        self.assertEqual(result['state'],'done')
        self.assertEqual(self.manifest()['previous_attempt']['batch_id'],'batch-1')
        self.assertEqual(self.manifest()['previous_attempt']['manifest_sha256'],reader.digest(old))
        self.assertEqual(old.read_bytes(),original); self.assertEqual(upload.call_count,2)
        self.assert_clean()

    def test_cannot_archive_unknown_task(self):
        self.request([reader.requests.Timeout('ambiguous')])
        with self.assertRaises(reader.requests.Timeout):reader.ingest(self.args)
        folder=self.folder(); before=(folder/'manifest.json').read_bytes()
        with self.assertRaises(diag.DiagnosticError):reader.archive_failed(self.args.cache_root,self.manifest()['cache_id'])
        self.assertEqual((folder/'manifest.json').read_bytes(),before)

    def test_zip_http_failure_keeps_batch_and_partial_attempts(self):
        req,upload=self.request([self.post(),self.poll(),self.poll()],zip_response=Response(503))
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assert_category('ZIP_DOWNLOAD_FAILED'); self.assertEqual(self.manifest()['last_error']['http_status'],503)
        with patch.object(reader.requests,'get',return_value=Response(content=self.zip())):
            self.assertEqual(reader.ingest(self.args)['state'],'done')
        self.assertEqual(upload.call_count,1); self.assertEqual([c.args[0] for c in req.call_args_list],['POST','GET','GET'])
        self.assert_clean()

    def test_corrupt_zip_preserved_then_explicit_redownload(self):
        req,upload=self.request([self.post(),self.poll(),self.poll()],zip_response=Response(content=b'bad ZIP evidence'))
        with self.assertRaises(zipfile.BadZipFile):reader.ingest(self.args)
        self.assert_category('ZIP_EXTRACT_FAILED')
        folder=self.folder(); self.assertEqual((folder/'result.zip').read_bytes(),b'bad ZIP evidence')
        self.args.redownload_result=True
        with patch.object(reader.requests,'get',return_value=Response(content=self.zip())):
            self.assertEqual(reader.ingest(self.args)['state'],'done')
        self.assertEqual(next((folder/'artifacts').glob('*.zip')).read_bytes(),b'bad ZIP evidence')
        self.assertEqual(upload.call_count,1)

    def test_missing_outputs_is_validation_error(self):
        self.request([self.post(),self.poll()],zip_response=Response(content=self.zip(invalid=True)))
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assert_category('ZIP_VALIDATION_FAILED')
        self.assertTrue((self.folder()/'result.zip').exists())

    def test_completed_cache_and_original_query_are_offline(self):
        req,upload=self.request([self.post(),self.poll()])
        result=reader.ingest(self.args)
        with patch.object(reader,'get_token',side_effect=AssertionError('No token access')):
            reader.ingest(self.args)
            reader.resolve_original(self.args.cache_root,result['cache_id'])
        self.assertEqual(req.call_count,2);self.assertEqual(upload.call_count,1)
        self.assertFalse(any('logs/' in p for p in self.manifest()['files']))
        self.assertTrue(any(e['event']=='output_file' for e in self.events()))
        self.assert_clean()

    def test_wrong_task_id_not_accepted(self):
        self.request([self.post(),self.poll('failed',data_id='some-other-document',err_msg='wrong paper')])
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assert_category('API_RESPONSE_INVALID')
        self.assertEqual(self.manifest()['state'],'submitted')

    def test_cli_error_omits_raw_exception_and_prints_log(self):
        self.request([reader.requests.Timeout('Authorization: Bearer '+TOKEN+' '+SIGNED)])
        out=io.StringIO()
        with patch('sys.argv',['reader.py',str(self.pdf),'--cache-root',self.args.cache_root]),patch('sys.stdout',out):
            with self.assertRaises(SystemExit):reader.main()
        result=json.loads(out.getvalue())
        self.assertEqual(result['error']['category'],'API_TRANSPORT')
        self.assertTrue(Path(result['diagnostic_log']).is_file())
        self.assertNotIn(TOKEN,out.getvalue());self.assertNotIn(SIGNED,out.getvalue())

    def test_api_envelope_error_and_malformed_json(self):
        self.request([Response(data={'code':'E_GENERAL','msg':'token=PRIVATE '+SIGNED,'extra':'UNEXPECTED-RESPONSE-SECRET'})])
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assert_category('API_REJECTED');self.assertEqual(self.manifest()['state'],'submission_unknown')
        self.assert_clean()

    def test_error_before_cache_id_has_unassigned_log(self):
        self.args.source=str(self.base/'missing.pdf')
        with self.assertRaises(FileNotFoundError) as error:reader.ingest(self.args)
        self.assertIn('unassigned',error.exception.diagnostic_log)
        self.assertTrue(Path(error.exception.diagnostic_log).exists())
        self.assertFalse((self.base/'cache/documents').exists())

    def test_orphan_cache_does_not_overwrite_evidence(self):
        import hashlib
        params={'model_version':'vlm','enable_formula':True,'enable_table':True,'language':'ch','is_ocr':False}
        key=reader.digest(self.pdf)+'-'+hashlib.sha256(json.dumps(params,sort_keys=True).encode()).hexdigest()[:16]
        folder=self.base/'cache/documents'/key;folder.mkdir(parents=True)
        (folder/'source.pdf').write_bytes(b'existing evidence')
        with patch.object(reader,'api') as api, self.assertRaisesRegex(diag.DiagnosticError,'no manifest'):
            reader.ingest(self.args)
        api.assert_not_called()
        self.assertEqual((folder/'source.pdf').read_bytes(),b'existing evidence')
        self.assertFalse((folder/'manifest.json').exists())

    def test_finalizer_io_error_does_not_mask_task_failure(self):
        self.request([self.post(),self.poll('failed',err_msg='parsing failed')])
        emit=diag.Run.emit
        def fail_inventory(run,name,**fields):
            if name=='output_file':raise OSError('simulated disk error')
            return emit(run,name,**fields)
        with patch.object(diag.Run,'emit',fail_inventory):
            with self.assertRaises(diag.DiagnosticError) as caught:reader.ingest(self.args)
        self.assertEqual(caught.exception.category,'TASK_FAILED')
        self.assertTrue(caught.exception.safe_details['diagnostics_incomplete'])
        self.assertEqual(self.manifest()['last_error']['category'],'TASK_FAILED')

    def test_history_and_success_files_survive_multiple_runs(self):
        self.request([self.post(),self.poll()])
        r=reader.ingest(self.args)
        reader.ingest(self.args); reader.resolve_original(self.args.cache_root,r['cache_id'])
        logs=self.manifest()['diagnostic_logs']
        self.assertEqual(len(set(logs)),3)
        self.assertTrue(all((self.base/'cache'/p).exists() for p in logs))
        reader.verify_cache(self.folder(),self.manifest())

    def test_malformed_response_and_poll_auth(self):
        self.request([self.post(),Response(data=ValueError('malformed '+TOKEN))])
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assert_category('API_RESPONSE_INVALID')
        self.assertEqual(self.manifest()['state'],'submitted')
        self.request([Response(403)])
        with self.assertRaises(reader.TokenRejected):reader.ingest(self.args)
        self.assert_category('API_AUTH_REJECTED');self.assertEqual(self.manifest()['state'],'submitted')
        self.assert_clean()

    def test_missing_batch_in_submitted_state_never_posts(self):
        req,_=self.request([self.post(),self.poll('running')])
        reader.ingest(self.args)
        m=self.manifest();m.pop('batch_id');reader.save(self.folder()/'manifest.json',m)
        self.args.recover=True
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        self.assertEqual(req.call_count,2);self.assert_category('AMBIGUOUS_SUBMISSION')

    def test_manual_archive_name_requires_explicit_link(self):
        req,_=self.request([self.post(),self.poll('failed')])
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        failed=self.base/'cache/failed/ch4-attempt1';failed.parent.mkdir()
        self.folder().rename(failed)
        with self.assertRaises(diag.DiagnosticError) as caught:reader.ingest(self.args)
        self.assertEqual(caught.exception.category,'RETRY_LINK_REQUIRED');self.assertEqual(req.call_count,2)

    def test_retry_from_wrong_pdf_blocked(self):
        req,_=self.request([self.post(),self.poll('failed')])
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        archived=reader.archive_failed(self.args.cache_root,self.manifest()['cache_id'])
        self.pdf.write_bytes(b'%PDF-1.4\nDIFFERENT revision')
        self.args.retry_from=archived['retry_from']
        with self.assertRaises(diag.DiagnosticError) as caught:reader.ingest(self.args)
        self.assertEqual(caught.exception.category,'RETRY_BLOCKED');self.assertEqual(req.call_count,2)

    def test_zip_interrupted_download_retained(self):
        r=Response();
        def chunks(n):
            yield b'partial ZIP'
            raise reader.requests.Timeout(SIGNED)
        r.iter_content=chunks
        self.request([self.post(),self.poll()],zip_response=r)
        with self.assertRaises(reader.requests.Timeout):reader.ingest(self.args)
        self.assert_category('ZIP_DOWNLOAD_FAILED')
        self.assertEqual(next((self.folder()/'downloads').glob('*.zip')).read_bytes(),b'partial ZIP')
        self.assertFalse((self.folder()/'result.zip').exists());self.assert_clean()

    def test_manifest_identity_mismatch_blocks_access(self):
        req,_=self.request([self.post(),self.poll('running')])
        reader.ingest(self.args)
        folder=self.folder();m=self.manifest();key=m['cache_id']
        m['cache_id']='../outside';reader.save(folder/'manifest.json',m)
        before=(folder/'manifest.json').read_bytes()
        with self.assertRaises(diag.DiagnosticError):reader.ingest(self.args)
        with self.assertRaises(diag.DiagnosticError):reader.resolve_original(self.args.cache_root,key)
        self.assertEqual(req.call_count,2)
        self.assertEqual((folder/'manifest.json').read_bytes(),before)

if __name__=='__main__': unittest.main()
