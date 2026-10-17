"""Безопасное создание и открытие приватных каталогов."""
from __future__ import annotations

import os
import stat
from pathlib import Path


def open_private_directory(path: Path, *, create: bool = False) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open("/" if path.is_absolute() else ".", flags)
    try:
        for component in path.parts:
            if component in ("/", "."):
                continue
            if component == "..":
                raise ValueError(f"Путь не должен содержать '..': {path}")
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd

        directory_stat = os.fstat(directory_fd)
        if directory_stat.st_uid != os.geteuid():
            raise PermissionError(
                f"Каталог должен принадлежать текущему пользователю: {path}"
            )
        if stat.S_IMODE(directory_stat.st_mode) & 0o077:
            raise PermissionError(
                f"Каталог доступен группе или другим пользователям: {path}"
            )
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise
