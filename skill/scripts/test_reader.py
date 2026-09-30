import argparse
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import reader

class ReaderTests(unittest.TestCase):
    def test_auth_rejection_and_other_errors(self):
        from unittest.mock import Mock
        for status, code in [(401, None), (403, None), (200, 'A0202'), (200, 'A0211')]:
            response = Mock(status_code=status)
            response.json.return_value = {'code': code}
            with patch.object(reader.requests, 'request', return_value=response):
                with self.assertRaises(reader.TokenRejected):
                    reader.api('GET', '/extract-results/batch/example', 'test')
        response = Mock(status_code=429)
        with patch.object(reader.requests, 'request', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'HTTP status 429'):
                reader.api('GET', '/extract-results/batch/example', 'test')

    def test_reminder_date(self):
        from reminder import plan
        result = plan('2026-12-29')
        self.assertEqual(result['remind_at'], '2026-12-28T09:00:00+08:00')
        self.assertEqual(result['status'], 'pending_scheduler')

    def test_pending_resumes_without_second_post(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            (p/'paper.pdf').write_bytes(b'%PDF-1.4\nfixture')
            args = argparse.Namespace(source=str(p/'paper.pdf'), cache_root=str(p/'cache'), project=None, title='', language='en', ocr=False, wait=0, recover=False)
            class Response:
                status_code = 200
            responses = [{'batch_id': 'batch', 'file_urls': ['https://example.org/upload']}, {'extract_result': [{'state': 'running'}]}, {'extract_result': [{'state': 'running'}]}]
            with patch.object(reader, 'get_token', return_value='fake'), patch.object(reader, 'api', side_effect=responses) as api, patch.object(reader.requests, 'put', return_value=Response()) as upload:
                self.assertEqual(reader.ingest(args)['state'], 'pending')
                self.assertEqual(reader.ingest(args)['state'], 'pending')
                self.assertEqual(upload.call_count, 1)
                self.assertEqual([c.args[0] for c in api.call_args_list], ['POST', 'GET', 'GET'])

    def test_zip_traversal(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            with zipfile.ZipFile(p/'bad.zip', 'w') as z:
                z.writestr('../escape', 'bad')
            with self.assertRaises(RuntimeError):
                reader.extract(p/'bad.zip', p/'out')
            self.assertFalse((p/'escape').exists())

    def test_lifecycle_cache_and_integrity(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            pdf = p/'paper.pdf'
            pdf.write_bytes(b'%PDF-1.4\nfixture')
            args = argparse.Namespace(source=str(pdf), cache_root=str(p/'cache'), project=str(p/'project'), title='Paper', language='en', ocr=False, wait=0, recover=False)
            class Response:
                status_code = 200
            def fake_api(method, path, token, payload=None):
                self.assertEqual(token, 'fake')
                if method == 'POST':
                    self.assertEqual(payload['model_version'], 'vlm')
                    return {'batch_id': 'batch', 'file_urls': ['https://storage.example/upload']}
                return {'extract_result': [{'state': 'done', 'full_zip_url': 'https://storage.example/result'}]}
            def fake_download(url, dest, limit):
                with zipfile.ZipFile(dest, 'w') as z:
                    z.writestr('full.md', '# Paper\nFormula $x^2$')
                    z.writestr('content_list.json', '[]')
            with patch.object(reader, 'get_token', return_value='fake'), patch.object(reader, 'api', side_effect=fake_api) as api, patch.object(reader.requests, 'put', return_value=Response()), patch.object(reader, 'download', side_effect=fake_download):
                result = reader.ingest(args)
                self.assertEqual(result['state'], 'done')
                self.assertEqual(api.call_count, 2)
                self.assertEqual(reader.ingest(args)['cache_id'], result['cache_id'])
                self.assertEqual(api.call_count, 2)
                index = reader.read(p/'project/literature/index.json')
                self.assertIn(result['cache_id'], index['documents'])
                Path(result['markdown'][0]).write_text('tampered')
                with self.assertRaisesRegex(RuntimeError, 'CACHE_DAMAGED'):
                    reader.ingest(args)

    def test_reject_html(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'fake.pdf'
            p.write_text('<html>login</html>')
            args = argparse.Namespace(source=str(p), cache_root=str(Path(t)/'cache'))
            with self.assertRaisesRegex(RuntimeError, 'Not a PDF'):
                reader.ingest(args)

    def test_missing_token_preserves_source(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'paper.pdf'
            p.write_bytes(b'%PDF-1.4\nfixture')
            args = argparse.Namespace(source=str(p), cache_root=str(Path(t)/'cache'), language='en', ocr=False)
            with patch.object(reader, 'get_token', return_value=''):
                with self.assertRaisesRegex(RuntimeError, 'not configured'):
                    reader.ingest(args)
            self.assertEqual(len(list((Path(t)/'cache').rglob('source.pdf'))), 1)

if __name__ == '__main__':
    unittest.main()
