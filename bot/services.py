"""Каталог служений и форматы их блоков в книге Excel."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ServiceDefinition:
    id: str
    name: str
    aliases: tuple[str, ...]
    assignment_layout: Literal["pairs", "single"]


SERVICES = (
    ServiceDefinition("assembly", "Собрание", ("собрание",), "pairs"),
    ServiceDefinition("sound", "Звук", ("звук",), "pairs"),
    ServiceDefinition("rehearsal", "Спевка", ("спевка",), "single"),
    ServiceDefinition("slides", "Слайды", ("слайды",), "single"),
    ServiceDefinition("broadcast", "Трансляция", ("трансляция",), "pairs"),
    ServiceDefinition(
        "order", "Орг. порядка", ("орг. порядка", "орг порядка"), "pairs"
    ),
    ServiceDefinition("kitchen", "Кухня", ("кухня",), "pairs"),
    ServiceDefinition("coffee", "Кофе", ("кофе",), "pairs"),
)

SERVICES_BY_ID = {service.id: service for service in SERVICES}
