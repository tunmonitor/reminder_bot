import http.client
import json
import logging
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from bot.config import load_webapp_config
from bot.webapp_server import _WebAppHandler


class WebAppConfigTest(unittest.TestCase):
    def test_reads_required_host_and_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "webapp_config.json"
            config_path.write_text(
                json.dumps({"host": "127.0.0.1", "port": 8123}), "utf-8"
            )
            config = load_webapp_config(root, config_path)
        self.assertEqual((config.host, config.port), ("127.0.0.1", 8123))

    def test_rejects_missing_invalid_port(self):
        for port in (None, True, 0, 65536, "8080"):
            with self.subTest(port=port), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "webapp_config.json"
                data = {"host": "127.0.0.1"}
                if port is not None:
                    data["port"] = port
                path.write_text(json.dumps(data), "utf-8")
                with self.assertRaisesRegex(RuntimeError, "'port'"):
                    load_webapp_config(root, path)


class WebAppStaticServerTest(unittest.TestCase):
    def test_only_serves_files_within_webapp_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root = project / "webapp"
            root.mkdir()
            (root / "index.html").write_text("hello mini app", "utf-8")
            (project / "secret.env").write_text("private", "utf-8")
            handler = lambda *args, **kwargs: _WebAppHandler(
                *args,
                directory=str(root),
                logger=logging.getLogger("test.webapp"),
                **kwargs,
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_address[1], timeout=3
                )
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), b"hello mini app")
                connection.request("GET", "/%2e%2e/secret.env")
                response = connection.getresponse()
                self.assertEqual(response.status, 404)
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
