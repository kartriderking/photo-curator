"""Duplicate launches must not run startup or alter tasks; bind before startup."""
import json
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from launch_web import existing_app, main


class LaunchWebTest(unittest.TestCase):
    def setUp(self):
        self.mode = 'current'
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/api/health' and owner.mode == 'legacy':
                    self.send_error(404)
                    return
                if self.path == '/api/health':
                    data = json.dumps({'app': 'photo-curator' if owner.mode == 'current' else 'other'}).encode()
                elif self.path == '/':
                    data = '<title>轻相册 · 本地照片整理</title>'.encode()
                else:
                    data = b'{"jobs":[],"progress":{}}'
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_port
        self.url = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_duplicate_launch_opens_existing_page_without_server_startup(self):
        with patch('uvicorn.Server', side_effect=AssertionError('duplicate startup')), patch('launch_web.webbrowser.open') as browser:
            self.assertEqual(main(['--port', str(self.port)]), 0)
        browser.assert_called_once_with(self.url)

    def test_legacy_instance_is_recognized_without_restarting_it(self):
        self.mode = 'legacy'
        self.assertTrue(existing_app(self.url))
        with patch('uvicorn.Server', side_effect=AssertionError('duplicate startup')):
            self.assertEqual(main(['--port', str(self.port), '--no-browser']), 0)

    def test_foreign_service_is_not_reused_or_stopped(self):
        self.mode = 'other'
        with patch('uvicorn.Server', side_effect=AssertionError('port is occupied')), patch('launch_web.webbrowser.open') as browser:
            self.assertEqual(main(['--port', str(self.port), '--no-browser']), 1)
        browser.assert_not_called()
        self.assertFalse(existing_app(self.url))

    def test_port_is_reserved_before_startup(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        def run(*, sockets):
            self.assertEqual(sockets[0].getsockname()[1], port)
            with socket.socket() as competing:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    competing.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                with self.assertRaises(OSError):
                    competing.bind(('127.0.0.1', port))
        with patch('uvicorn.Server') as factory:
            factory.return_value.run.side_effect = run
            factory.return_value.started = True
            self.assertEqual(main(['--port', str(port), '--no-browser']), 0)

    def test_failed_startup_returns_an_error_for_batch_pause(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        with patch('uvicorn.Server') as factory:
            factory.return_value.started = False
            self.assertEqual(main(['--port', str(port), '--no-browser']), 1)


if __name__ == '__main__':
    unittest.main()
