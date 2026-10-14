"""Small standard-library HTTP server for the Telegram Web App static files."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import load_webapp_config


class _WebAppHandler(SimpleHTTPRequestHandler):
    server_version = "ReminderBotWebApp"

    def __init__(self, *args, directory: str, logger: logging.Logger, **kwargs):
        self._root = Path(directory).resolve(strict=True)
        self._logger = logger
        super().__init__(*args, directory=str(self._root), **kwargs)

    def translate_path(self, path: str) -> str:
        relative = unquote(urlsplit(path).path).lstrip("/")
        if "\x00" in relative or "\\" in relative:
            return str(self._root / ".not-found")
        candidate = self._root.joinpath(*Path(relative).parts)
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self._root)
        except (OSError, ValueError):
            return str(self._root / ".not-found")
        return str(resolved)

    def list_directory(self, path):
        self.send_error(403, "Directory listing is disabled")
        return None

    def log_message(self, fmt, *args):
        self._logger.info("%s %s", self.command, urlsplit(self.path).path)


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="bot.webapp_server")
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    project_dir = args.project_dir.resolve(strict=True)
    config = load_webapp_config(project_dir, args.config)
    root = project_dir / "webapp"
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError(f"Каталог Web App отсутствует или небезопасен: {root}")
    root = root.resolve(strict=True)
    if not (root / "index.html").is_file():
        raise RuntimeError(f"Не найдена входная страница Web App: {root / 'index.html'}")
    if args.check:
        return 0

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stdout,
    )
    logger = logging.getLogger("reminder_bot.webapp")
    handler = lambda *a, **kw: _WebAppHandler(
        *a, directory=str(root), logger=logger, **kw
    )
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    logger.info("Web App HTTP server listening on %s:%s", config.host, config.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Web App HTTP server stopping")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        logging.exception("Web App server failed")
        sys.exit(1)
