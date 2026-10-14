import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock

from bot.config import load_webapp_config
from bot.webapp_server import _WebAppHandler, main


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
        self.assertIsNone(config.public_url)

    def test_reads_public_https_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "webapp_config.json"
            path.write_text(
                json.dumps({
                    "host": "127.0.0.1",
                    "port": 8123,
                    "public_url": "https://bot.example.org/app",
                }),
                "utf-8",
            )
            config = load_webapp_config(root, path)
        self.assertEqual(config.public_url, "https://bot.example.org/app")

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

    def test_rejects_non_https_public_url(self):
        for url in (
            "http://example.org/app",
            "https://user@example.org/app",
            "https://example.org:invalid/app",
        ):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "webapp_config.json"
                path.write_text(
                    json.dumps({
                        "host": "127.0.0.1",
                        "port": 8080,
                        "public_url": url,
                    }),
                    "utf-8",
                )
                with self.assertRaisesRegex(RuntimeError, "public_url"):
                    load_webapp_config(Path(tmp), path)


class WebAppStaticServerTest(unittest.TestCase):
    def test_only_serves_files_within_webapp_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root = project / "webapp"
            root.mkdir()
            (root / "index.html").write_text("hello mini app", "utf-8")
            (project / "secret.env").write_text("private", "utf-8")
            logger = MagicMock()
            handler = lambda *args, **kwargs: _WebAppHandler(
                *args,
                directory=str(root),
                logger=logger,
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
                self.assertNotEqual(response.read(), b"private")
                logger.info.assert_any_call("%s %s %s", "GET", "/", "200")
                logger.info.assert_any_call(
                    "%s %s %s", "GET", "/%2e%2e/secret.env", "404"
                )
                logger.warning.assert_called()
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_root_index_symlink_cannot_serve_outside_webapp(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root = project / "webapp"
            root.mkdir()
            (project / "private.html").write_text("private", "utf-8")
            (root / "index.html").symlink_to(project / "private.html")
            logger = MagicMock()
            handler = lambda *args, **kwargs: _WebAppHandler(
                *args,
                directory=str(root),
                logger=logger,
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
                self.assertEqual(response.status, 404)
                self.assertNotEqual(response.read(), b"private")
                connection.close()
                logger.warning.assert_called()
                self.assertIn("404", str(logger.warning.call_args))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_startup_rejects_symlinked_index_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            root = project / "webapp"
            root.mkdir()
            (project / "private.html").write_text("private", "utf-8")
            (root / "index.html").symlink_to(project / "private.html")
            config = project / "webapp_config.json"
            config.write_text(
                json.dumps({"host": "127.0.0.1", "port": 8080}),
                "utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "входная страница"):
                main(["--project-dir", str(project), "--config", str(config), "--check"])


if __name__ == "__main__":
    unittest.main()
