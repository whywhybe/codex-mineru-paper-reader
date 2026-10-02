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


class OriginalStorageTests(unittest.TestCase):
    PDF = b'%PDF-1.4\noriginal fixture'

    def complete(self, base, network=False, archived_pdf=True, before_result=None):
        from unittest.mock import Mock
        pdf = base / 'input.pdf'
        pdf.write_bytes(self.PDF)
        args = argparse.Namespace(source='https://example.org/paper.pdf' if network else str(pdf),
            cache_root=str(base/'cache'), project=str(base/'project'), title='Paper',
            language='en', ocr=False, wait=0, recover=False)
        def download(url, dest, limit=200*1024*1024):
            if url.endswith('paper.pdf'):
                dest.write_bytes(self.PDF)
                return url
            if before_result:
                before_result(pdf)
            with zipfile.ZipFile(dest, 'w') as z:
                z.writestr('full.md', '# Paper')
                z.writestr('content_list.json', '[]')
                z.writestr('images/figure.png', b'image fixture')
                z.writestr('layout.pdf', b'%PDF-1.4\ndifferent annotated PDF')
                if archived_pdf is not False:
                    data = self.PDF if archived_pdf is True else archived_pdf
                    z.writestr('nested/paper_origin.pdf', data)
                    z.writestr('second_origin.pdf', data)
        responses = [{'batch_id': 'batch', 'file_urls': ['https://example.org/upload']},
            {'extract_result': [{'state': 'done', 'full_zip_url': 'https://example.org/result.zip'}]}]
        with patch.object(reader, 'api', side_effect=responses), patch.object(reader, 'get_token', return_value='fake'), patch.object(reader.requests, 'put', return_value=Mock(status_code=200)), patch.object(reader, 'download', side_effect=download):
            result = reader.ingest(args)
        return pdf, args, result, Path(result['directory'])

    def test_local_retention_and_offline_reuse(self):
        with tempfile.TemporaryDirectory() as t:
            pdf, args, result, folder = self.complete(Path(t))
            self.assertEqual(Path(result['original_pdf']), pdf.resolve())
            self.assertEqual(pdf.read_bytes(), self.PDF)
            self.assertFalse((folder/'source.pdf').exists())
            self.assertFalse((folder/'mineru_raw/nested/paper_origin.pdf').exists())
            self.assertFalse((folder/'mineru_raw/second_origin.pdf').exists())
            self.assertTrue((folder/'mineru_raw/layout.pdf').is_file())
            self.assertTrue((folder/'mineru_raw/images/figure.png').is_file())
            with patch.object(reader, 'get_token', side_effect=AssertionError('no token')), patch.object(reader, 'api', side_effect=AssertionError('no API')):
                self.assertEqual(reader.ingest(args)['original_pdf'], str(pdf.resolve()))
                self.assertEqual(reader.resolve_original(args.cache_root, result['cache_id'])['original_pdf'], str(pdf.resolve()))

    def test_network_keeps_one_extracted_original(self):
        with tempfile.TemporaryDirectory() as t:
            _, args, result, folder = self.complete(Path(t), network=True)
            self.assertFalse((folder/'source.pdf').exists())
            self.assertEqual(Path(result['original_pdf']).read_bytes(), self.PDF)
            originals = [p for p in folder.rglob('*.pdf') if p.read_bytes() == self.PDF]
            self.assertEqual(len(originals), 1)
            self.assertTrue(originals[0].is_relative_to(folder/'mineru_raw'))
            reader.verify_cache(folder, reader.read(folder/'manifest.json'))

    def test_missing_or_changed_local_restores_without_upload(self):
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as t:
                pdf, args, result, folder = self.complete(Path(t))
                if changed:
                    pdf.write_bytes(b'%PDF-1.4\nnew revision')
                else:
                    pdf.unlink()
                with patch.object(reader, 'get_token', side_effect=AssertionError('no token')), patch.object(reader, 'api', side_effect=AssertionError('no API')):
                    restored = Path(reader.resolve_original(args.cache_root, result['cache_id'])['original_pdf'])
                    self.assertEqual(restored.read_bytes(), self.PDF)
                    self.assertEqual(restored.parent, folder)
                    self.assertEqual(reader.resolve_original(args.cache_root, result['cache_id'])['original_pdf'], str(restored))
                if changed:
                    self.assertEqual(pdf.read_bytes(), b'%PDF-1.4\nnew revision')
                else:
                    self.assertFalse(pdf.exists())
                reader.verify_cache(folder, reader.read(folder/'manifest.json'))

    def test_no_identical_zip_original_preserves_snapshot(self):
        for archive_value in (False, b'%PDF-1.4\nwrong version'):
            for network in (False, True):
                with self.subTest(value=archive_value, network=network), tempfile.TemporaryDirectory() as t:
                    pdf, args, result, folder = self.complete(Path(t), network=network, archived_pdf=archive_value)
                    self.assertEqual((folder/'source.pdf').read_bytes(), self.PDF)
                    pdf.unlink()
                    original = reader.resolve_original(args.cache_root, result['cache_id'])['original_pdf']
                    self.assertEqual(Path(original), folder/'source.pdf')

    def test_input_changes_during_parse_keeps_extracted_original(self):
        with tempfile.TemporaryDirectory() as t:
            pdf, args, result, folder = self.complete(Path(t), before_result=lambda p: p.write_bytes(b'changed'))
            self.assertEqual(pdf.read_bytes(), b'changed')
            self.assertEqual(Path(result['original_pdf']).read_bytes(), self.PDF)
            self.assertFalse((folder/'source.pdf').exists())

    def test_corrupt_zip_blocks_recovery(self):
        with tempfile.TemporaryDirectory() as t:
            pdf, args, result, folder = self.complete(Path(t))
            pdf.unlink()
            (folder/'result.zip').write_bytes(b'corrupted')
            with self.assertRaisesRegex(RuntimeError, 'CACHE_DAMAGED'):
                reader.resolve_original(args.cache_root, result['cache_id'])
            self.assertFalse((folder/'restored-original.pdf').exists())

    def test_legacy_done_cache_is_not_cleaned(self):
        with tempfile.TemporaryDirectory() as t:
            pdf, args, result, folder = self.complete(Path(t), archived_pdf=False)
            m = reader.read(folder/'manifest.json')
            for name in ('original', 'source_kind', 'local_source'):
                m.pop(name, None)
            reader.save(folder/'manifest.json', m)
            reader.ingest(args)
            self.assertTrue((folder/'source.pdf').exists())
            self.assertNotIn('original', reader.read(folder/'manifest.json'))

    def test_cleanup_interruption_is_resumable(self):
        with tempfile.TemporaryDirectory() as t:
            base = Path(t)
            unlink = Path.unlink
            def fail_source(path, *a, **kw):
                if path.name == 'source.pdf' and path.parent.parent.name == 'documents':
                    raise OSError('simulated interrupted cleanup')
                return unlink(path, *a, **kw)
            with patch.object(Path, 'unlink', fail_source), self.assertRaises(OSError):
                self.complete(base)
            folder = next((base/'cache/documents').iterdir())
            m = reader.read(folder/'manifest.json')
            self.assertEqual(m['state'], 'done')
            self.assertTrue(m['original_cleanup_pending'])
            result = reader.resolve_original(base/'cache', m['cache_id'])
            self.assertEqual(Path(result['original_pdf']), (base/'input.pdf').resolve())
            self.assertFalse((folder/'source.pdf').exists())
            self.assertNotIn('original_cleanup_pending', reader.read(folder/'manifest.json'))

    def test_invalid_cache_id(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaisesRegex(RuntimeError, 'Invalid cache ID'):
                reader.resolve_original(t, '../escape')

if __name__ == '__main__':
    unittest.main()
