"""Отчёт администраторам служения о напоминании (этап 3.2).

Отчёт уходит в личные чаты админов служения после группового напоминания
и редактируется, когда участники отвечают «Принял» / «Не смогу». Один
отчёт на админа и дату события: повторные напоминания той же даты
обновляют его, а не присылают новый.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date

from .presentation import (
    format_task_assignments,
    task_assignment_names,
    task_is_informational,
)
from .service_registry import DEFAULT_ADMIN_REPORT_TEMPLATE
from .ui import render_edit_screen, render_screen_chunks, send_telegram_screen

_TRUNCATED_NOTE = "… отчёт сокращён: не помещается в одно сообщение"

_CONFIRMATION_LABELS = {
    "confirmed": "✅ подтвердил(а)",
    "declined": "🚫 не сможет",
}


class _Values(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _people(assignments: dict) -> list[tuple[str, str]]:
    return [
        (task, name)
        for task, task_info in assignments.items()
        if not task_is_informational(task_info)
        for name in task_assignment_names(task_info)
    ]


def build_report_text(
    template: str | None,
    service_name: str,
    event_date: date,
    assignments: dict,
    contacts_by_name: dict[tuple[str, str], dict],
    notifications_enabled: Callable[[int], bool],
    confirmations: dict[tuple[str, str], str],
) -> str:
    people = _people(assignments)
    delivery = []
    answers = []
    for task, name in people:
        contact = contacts_by_name.get((task, name))
        telegram_id = contact.get("telegram_id") if contact else None
        if not telegram_id:
            delivery.append(f"⚪ {name} ({task}) — не привязан")
        elif not notifications_enabled(telegram_id):
            delivery.append(f"🔕 {name} ({task}) — уведомления выключены")
        else:
            delivery.append(f"✅ {name} ({task}) — уведомления включены")
        answers.append(
            f"{_CONFIRMATION_LABELS.get(confirmations.get((task, name)), '⏳ нет ответа')}"
            f" — {name} ({task})"
        )
    return (template or DEFAULT_ADMIN_REPORT_TEMPLATE).format_map(
        _Values(
            service_name=service_name,
            sunday_date=f"{event_date:%d.%m.%Y}",
            task_assignments=format_task_assignments(
                assignments, contacts_by_name, mark_unlinked=True
            ),
            delivery_status="\n".join(delivery) or "⚪ нет назначений",
            confirmation_status="\n".join(answers) or "⚪ нет назначений",
        )
    )


def fit_one_screen(text: str, limit: int) -> str:
    """Отчёт редактируется на месте, поэтому должен влезать в один экран."""
    if len(render_screen_chunks(text, limit=limit)) == 1:
        return text
    lines = text.splitlines()

    def candidate(count: int) -> str:
        return "\n".join([*lines[:count], _TRUNCATED_NOTE])

    # Наибольшее число начальных строк, которое ещё влезает в экран.
    low, high = 0, len(lines) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if len(render_screen_chunks(candidate(middle), limit=limit)) == 1:
            low = middle
        else:
            high = middle - 1
    return candidate(low)


def report_recipients(registry, service_id: str, fallback_admin_id: int) -> list[int]:
    """Админы служения; без них — суперадмин из настроек."""
    return registry.list_service_admins(service_id) or [fallback_admin_id]


def _report_text(registry, service: dict, event_date: date, assignments: dict,
                 admin_actor_id: int) -> str:
    contacts = registry.list_responsible_contacts(admin_actor_id, service["id"])
    return build_report_text(
        service.get("admin_report_template"),
        service["name"],
        event_date,
        assignments,
        {(item["task"], item["name"]): item for item in contacts},
        lambda telegram_id: registry.is_user_notifications_enabled(
            telegram_id, service["id"]
        ),
        registry.list_confirmations(service["id"], event_date.isoformat()),
    )


async def send_admin_reports(
    bot,
    registry,
    service: dict,
    event_date: date,
    assignments: dict,
    admin_actor_id: int,
    logger: logging.Logger,
    limit: int,
) -> None:
    """Отправляет отчёт админам или обновляет уже отправленный на эту дату."""
    text = fit_one_screen(
        _report_text(registry, service, event_date, assignments, admin_actor_id),
        limit,
    )
    stored = {
        report["admin_id"]: report
        for report in registry.list_admin_reports(
            service["id"], event_date.isoformat()
        )
    }
    for admin_id in report_recipients(registry, service["id"], admin_actor_id):
        report = stored.get(admin_id)
        try:
            if report is not None:
                await bot.edit_message_text(
                    render_edit_screen(text, limit=limit),
                    chat_id=admin_id,
                    message_id=report["message_id"],
                    parse_mode="HTML",
                )
                message_id = report["message_id"]
            else:
                sent = await send_telegram_screen(
                    bot.send_message, admin_id, text, limit=limit
                )
                message_id = getattr(sent, "message_id", None)
        except Exception as exc:  # noqa: BLE001
            if "message is not modified" in str(exc).lower():
                continue
            logger.warning(
                "Не удалось отправить отчёт админу %s по служению %s: %s",
                admin_id, service["id"], type(exc).__name__,
            )
            continue
        if message_id is not None:
            registry.save_admin_report(
                service["id"], event_date.isoformat(), admin_id, message_id,
                assignments,
            )


async def refresh_admin_reports(
    bot,
    registry,
    service_id: str,
    event_date: date,
    admin_actor_id: int,
    logger: logging.Logger,
    limit: int,
) -> None:
    """Перестраивает отправленные отчёты после ответа участника."""
    service = registry.get_service(service_id)
    reports = registry.list_admin_reports(service_id, event_date.isoformat())
    if service is None or not reports:
        return
    for report in reports:
        text = fit_one_screen(
            _report_text(
                registry, service, event_date, report["assignments"],
                admin_actor_id,
            ),
            limit,
        )
        try:
            await bot.edit_message_text(
                render_edit_screen(text, limit=limit),
                chat_id=report["admin_id"],
                message_id=report["message_id"],
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            if "message is not modified" not in str(exc).lower():
                logger.warning(
                    "Не удалось обновить отчёт админу %s по служению %s: %s",
                    report["admin_id"], service_id, type(exc).__name__,
                )


async def notify_admins_declined(
    bot,
    registry,
    service: dict,
    event_date: date,
    task: str,
    name: str,
    admin_actor_id: int,
    logger: logging.Logger,
) -> int:
    """Сообщает админам служения об ответе «Не смогу»; возвращает число
    доставленных сообщений."""
    text = (
        f"🚫 {name} не сможет участвовать: «{task}» · {service['name']} · "
        f"{event_date:%d.%m.%Y}"
    )
    delivered = 0
    for admin_id in report_recipients(registry, service["id"], admin_actor_id):
        try:
            await bot.send_message(admin_id, text, parse_mode=None)
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Не удалось сообщить админу %s об отказе по служению %s: %s",
                admin_id, service["id"], type(exc).__name__,
            )
    return delivered
