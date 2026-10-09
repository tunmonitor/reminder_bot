"""Скачивание публичного файла с Яндекс.Диска с retry-политикой."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp

YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"

RETRY_ATTEMPTS = 3
RETRY_INTERVAL = 300        # 5 минут, согласно ТЗ
HTTP_TIMEOUT_API = 30
HTTP_TIMEOUT_DOWNLOAD = 120


async def download_public(
    public_url: str,
    dest: Path,
    debug_logger: logging.Logger,
    attempts: int = RETRY_ATTEMPTS,
    interval: int = RETRY_INTERVAL,
) -> Path:
    """Скачивает xlsx по публичной ссылке в dest. Возвращает dest."""
    last_err: Exception | None = None
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_DOWNLOAD)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, attempts + 1):
            try:
                # 1) Получаем href
                async with session.get(
                    YANDEX_API, params={"public_key": public_url},
                    timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_API),
                ) as resp:
                    debug_logger.debug(
                        "Yandex API status=%s attempt=%s", resp.status, attempt
                    )
                    if resp.status != 200:
                        body = await resp.text()
                        raise RuntimeError(
                            f"Yandex API вернул {resp.status}: {body[:200]}"
                        )
                    payload = await resp.json()
                href = payload.get("href")
                if not href:
                    raise RuntimeError("В ответе Yandex API нет поля 'href'")

                # 2) Скачиваем файл
                async with session.get(href) as resp:
                    debug_logger.debug("Yandex download status=%s", resp.status)
                    if resp.status != 200:
                        raise RuntimeError(
                            f"Скачивание вернуло {resp.status}"
                        )
                    content = await resp.read()
                    debug_logger.debug(
                        "Yandex downloaded %s bytes -> %s",
                        len(content), dest,
                    )
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(content)
                    return dest

            except Exception as exc:  # noqa: BLE001
                last_err = exc
                debug_logger.exception(
                    "Попытка %s/%s скачать Яндекс.Диск провалилась",
                    attempt, attempts,
                )
                if attempt < attempts:
                    await asyncio.sleep(interval)

    raise RuntimeError(
        f"Не удалось скачать файл с Яндекс.Диска за {attempts} попыток: {last_err}"
    )
