from io import BytesIO
import os
from pathlib import Path
import pwd
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from file_transfer import FileConflict, checked_request
from terminal import Terminal


@unittest.skipUnless(os.access('/usr/bin/python3', os.X_OK), 'file helper requires /usr/bin/python3')
class TerminalFileTransferTests(unittest.TestCase):
    def setUp(self):
        user = pwd.getpwuid(os.geteuid()).pw_name
        self.context = {'connection':'test-local', 'user':user}
        with patch('terminal.LOCAL_TERMINAL_ENABLED', True):
            self.terminal = Terminal({'kind':'local', 'id':'test-local', 'name':'Local',
                                      'host':'localhost', 'port':None, 'user':user})
        self.addCleanup(self.terminal.close)
        self.wait_for_prompt()
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def wait_for_prompt(self):
        with self.terminal.condition:
            self.assertTrue(self.terminal.condition.wait_for(lambda: self.terminal.ready, 5))

    def start(self, mode, **values):
        request = checked_request({'mode':mode, 'directory':str(self.directory), **values})
        job = self.terminal.start_files(self.context, request)
        return job, job.prepare()

    def test_binary_content_and_shell_context_survive_multiple_windows(self):
        original_context = self.terminal.context.copy()
        name = '测试包.jar'
        content = os.urandom(3 * 1024 * 1024 + 29)
        self.start('mkdir', name='archive')
        upload, _ = self.start('upload', name=name, size=len(content))
        upload.upload(BytesIO(content), len(content))
        self.assertTrue(upload.released.wait(5))
        self.assertEqual((self.directory / name).read_bytes(), content)
        download, _ = self.start('download', name=name)
        destination = BytesIO()
        download.claim('download')
        download.download(destination)
        self.assertEqual(destination.getvalue(), content)
        self.assertTrue(download.released.wait(5))
        _, listing = self.start('list')
        self.assertEqual([(entry['name'], entry['kind']) for entry in listing['entries']],
                         [('archive','directory'), (name,'file')])
        self.assertEqual(self.terminal.context, original_context)
        self.assertFalse(self.terminal.history)

    def test_cancelled_or_stale_overwrite_leaves_original_file(self):
        path = self.directory / 'portal.war'
        path.write_bytes(b'initial package')
        _, target = self.start('inspect', name=path.name)
        with self.assertRaises(FileConflict):
            self.start('upload', name=path.name, size=7)
        upload, _ = self.start('upload', name=path.name, size=7, overwrite=True, version=target['version'])
        with self.assertRaises(ValueError):
            upload.upload(BytesIO(b'short'), 7)
        self.assertTrue(upload.released.wait(5))
        self.assertEqual(path.read_bytes(), b'initial package')
        self.assertEqual(list(self.directory.glob('.workbench-upload-*')), [])
        path.write_bytes(b'changed externally')
        with self.assertRaises(FileConflict):
            self.start('upload', name=path.name, size=7, overwrite=True, version=target['version'])
        self.assertEqual(path.read_bytes(), b'changed externally')
