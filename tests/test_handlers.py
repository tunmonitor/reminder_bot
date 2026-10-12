import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bot.handlers import register, router
from bot.service_registry import ServiceRegistry


class ConfirmationHandlerTest(unittest.TestCase):
    def test_admin_test_command_sends_only_to_group_and_admin(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_test_previews=AsyncMock(
                    return_value={
                        "sunday_date": date(2026, 10, 11),
                        "group_text": "Группа: @{alex}",
                        "personal_messages": [
                            ("Ответственный", "Алексей", "Алексей, будь готов")
                        ],
                        "issues": [],
                    }
                ),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.message.handlers[-1].callback
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=67890),
                chat=SimpleNamespace(type="private"),
                answer=AsyncMock(),
            )

            await handler(message)

            self.assertEqual(context.bot.send_message.await_count, 4)
            self.assertEqual(
                context.bot.send_message.await_args_list[0].args[0], -100
            )
            group_text = context.bot.send_message.await_args_list[0].args[1]
            self.assertIn("ТЕСТ бота", group_text)
            self.assertIn("не запускалась", group_text)
            self.assertEqual(
                context.bot.send_message.await_args_list[1].args[0], 67890
            )
            report = context.bot.send_message.await_args_list[1].args[1]
            self.assertIn("Тестовое сообщение в группу: ✅ отправлено", report)
            self.assertIn("Личные сообщения участникам не отправлялись", report)
            group_preview = context.bot.send_message.await_args_list[2]
            self.assertEqual(group_preview.args[0], 67890)
            self.assertIn("Группа: @{alex}", group_preview.args[1])
            self.assertEqual(
                group_preview.kwargs["parse_mode"], "HTML"
            )
            personal_preview = context.bot.send_message.await_args_list[3]
            self.assertEqual(personal_preview.args[0], 67890)
            self.assertIn("Ответственный, Алексей", personal_preview.args[1])
            self.assertIn("Алексей, будь готов", personal_preview.args[1])
            context.prepare_test_previews.assert_awaited_once()
            message.answer.assert_not_awaited()

        asyncio.run(run())

    def test_test_command_rejects_non_admin_and_non_private_chat(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_test_previews=AsyncMock(),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.message.handlers[-1].callback

            unauthorized = SimpleNamespace(
                from_user=SimpleNamespace(id=12345),
                chat=SimpleNamespace(type="private"),
                answer=AsyncMock(),
            )
            await handler(unauthorized)
            unauthorized.answer.assert_not_awaited()

            group_chat = SimpleNamespace(
                from_user=SimpleNamespace(id=67890),
                chat=SimpleNamespace(type="group"),
                answer=AsyncMock(),
            )
            await handler(group_chat)
            group_chat.answer.assert_not_awaited()
            context.bot.send_message.assert_not_awaited()

        asyncio.run(run())

    def test_group_test_failure_is_reported_to_admin(self):
        async def run():
            from aiogram.exceptions import TelegramAPIError

            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                bot=SimpleNamespace(
                    send_message=AsyncMock(
                        side_effect=[
                            TelegramAPIError(
                                method=MagicMock(), message="group unavailable"
                            ),
                            None,
                            None,
                        ]
                    )
                ),
                prepare_test_previews=AsyncMock(
                    return_value={
                        "sunday_date": date(2026, 10, 11),
                        "group_text": "Полный текст группы",
                        "personal_messages": [],
                        "issues": [],
                    }
                ),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.message.handlers[-1].callback
            message = SimpleNamespace(
                from_user=SimpleNamespace(id=67890),
                chat=SimpleNamespace(type="private"),
                answer=AsyncMock(),
            )

            await handler(message)

            self.assertEqual(context.bot.send_message.await_count, 3)
            self.assertEqual(
                context.bot.send_message.await_args_list[1].args[0], 67890
            )
            self.assertIn(
                "ошибка (TelegramAPIError)",
                context.bot.send_message.await_args_list[1].args[1],
            )
            self.assertIn(
                "Полный текст группы",
                context.bot.send_message.await_args_list[2].args[1],
            )
            context.debug_logger.exception.assert_called_once()

        asyncio.run(run())

    def test_failed_group_update_can_be_retried_on_duplicate_callback(self):
        async def run():
            participant = {
                "name": "<Алексей>",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                bot=SimpleNamespace(
                    send_message=AsyncMock(
                        side_effect=[RuntimeError("temporary"), object()]
                    )
                ),
                group_chat_id=-100,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.callback_query.handlers[-1].callback
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await handler(callback)
            self.assertTrue(participant["confirmed"])
            self.assertFalse(participant["group_update_sent"])

            await handler(callback)

            self.assertTrue(participant["group_update_sent"])
            self.assertEqual(context.bot.send_message.await_count, 2)
            self.assertEqual(
                context.bot.send_message.await_args.kwargs,
                {"parse_mode": None},
            )
            self.assertIn(
                "Нажмите кнопку повторно",
                callback.answer.await_args_list[0].args[0],
            )

        asyncio.run(run())

    def test_group_update_save_failure_does_not_repeat_post_on_retry(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
            }
            state_cache = SimpleNamespace(
                state={
                    "sunday_date": "2026-10-11",
                    "participants": {"Ответственный": participant},
                },
                save=MagicMock(side_effect=[None, OSError("disk full"), None]),
            )
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                state_cache=state_cache,
                bot=SimpleNamespace(send_message=AsyncMock(return_value=object())),
                group_chat_id=-100,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.callback_query.handlers[-1].callback
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            with patch("bot.handlers._notify_admin", new=AsyncMock()) as notify_admin:
                await handler(callback)

            self.assertTrue(participant["group_update_sent"])
            self.assertFalse(participant["group_update_persisted"])
            self.assertIn("не удалось сохранить", callback.answer.await_args.args[0])
            context.bot.send_message.assert_awaited_once()
            notify_admin.assert_awaited_once()

            with patch("bot.handlers._notify_admin", new=AsyncMock()):
                await handler(callback)

            self.assertTrue(participant["group_update_persisted"])
            self.assertEqual(context.bot.send_message.await_count, 1)
            self.assertEqual(state_cache.save.call_count, 3)

        asyncio.run(run())

    def test_concurrent_callbacks_send_only_one_group_update(self):
        async def run():
            entered = asyncio.Event()
            release = asyncio.Event()

            async def send_message(*_args, **_kwargs):
                entered.set()
                await release.wait()

            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                confirmation_locks={},
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                bot=SimpleNamespace(send_message=AsyncMock(side_effect=send_message)),
                group_chat_id=-100,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.callback_query.handlers[-1].callback

            def callback():
                return SimpleNamespace(
                    data="cfm:2026-10-11:Ответственный",
                    from_user=SimpleNamespace(id=12345, username="alex"),
                    message=None,
                    answer=AsyncMock(),
                )

            first = callback()
            second = callback()
            first_task = asyncio.create_task(handler(first))
            await entered.wait()
            second_task = asyncio.create_task(handler(second))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first_task, second_task)

            self.assertEqual(context.bot.send_message.await_count, 1)
            self.assertTrue(participant["group_update_sent"])
            self.assertEqual(second.answer.await_args.args[0], "Вы уже подтвердили участие!")

        asyncio.run(run())


class ServiceManagementHandlerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.registry = ServiceRegistry(
            Path(self.temp_dir.name) / "registry.sqlite3"
        )
        self.registry.initialize(67890, -100)
        self.context = SimpleNamespace(
            interaction_logger=MagicMock(),
            debug_logger=MagicMock(),
            admin_id=67890,
            group_chat_id=-100,
            service_registry=self.registry,
            bot=SimpleNamespace(send_message=AsyncMock()),
        )
        self.dispatcher = SimpleNamespace(include_router=MagicMock())
        register(self.dispatcher, self.context)
        self.admin_router = self.dispatcher.include_router.call_args_list[0].args[0]

    @staticmethod
    def message(actor_id, text, chat_type="private"):
        return SimpleNamespace(
            from_user=SimpleNamespace(id=actor_id),
            chat=SimpleNamespace(type=chat_type),
            text=text,
            answer=AsyncMock(),
        )

    def test_super_admin_can_configure_chat_admin_and_contact(self):
        async def run():
            handlers = self.admin_router.message.handlers
            await handlers[2].callback(
                self.message(67890, "/service_admin_add sound 12345")
            )
            await handlers[4].callback(
                self.message(67890, "/service_chat sound -10022 on")
            )
            await handlers[5].callback(
                self.message(
                    67890,
                    "/service_contact sound Микшер | Алексей Иванов | "
                    "12345 | alex",
                )
            )
            await handlers[4].callback(
                self.message(67890, "/service_chat assembly -10077 off")
            )

            self.assertEqual(self.registry.list_service_admins("sound"), [12345])
            self.assertEqual(self.context.group_chat_id, -10077)
            service = next(
                service for service in self.registry.list_services()
                if service["id"] == "sound"
            )
            self.assertEqual(service["group_chat_id"], -10022)
            self.assertTrue(service["enabled"])
            assembly = self.registry.get_service("assembly")
            self.assertEqual(assembly["group_chat_id"], -10077)
            self.assertFalse(assembly["enabled"])
            self.assertEqual(
                self.registry.get_responsible_contact(
                    "sound", "Микшер", "Алексей Иванов"
                ),
                {"telegram_id": 12345, "username": "alex"},
            )

        asyncio.run(run())

    def test_service_admin_can_only_change_own_notification_settings(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            handler = self.admin_router.message.handlers[6].callback

            forbidden = self.message(
                12345,
                '/service_notify slides 2 17:30 | Текст группы | {}',
            )
            await handler(forbidden)
            self.assertIn(
                "администратору служения",
                forbidden.answer.await_args.args[0],
            )

            allowed = self.message(
                12345,
                '/service_notify sound 2 17:30 | Звук: {name} | '
                '{"Микшер": "{name}, проверьте звук"}',
            )
            await handler(allowed)
            allowed.answer.assert_awaited_once_with(
                "Настройки уведомлений сохранены.", parse_mode=None
            )
            service = next(
                service for service in self.registry.list_services()
                if service["id"] == "sound"
            )
            self.assertEqual(service["reminder_days"], 2)
            self.assertEqual(service["reminder_time"], "17:30")
            self.assertEqual(service["group_template"], "Звук: {name}")
            self.assertEqual(
                service["personal_templates"],
                {"Микшер": "{name}, проверьте звук"},
            )

        asyncio.run(run())

    def test_management_commands_are_silent_outside_private_chat(self):
        async def run():
            message = self.message(
                67890, "/service_admin_add sound 12345", chat_type="group"
            )
            await self.admin_router.message.handlers[2].callback(message)

            message.answer.assert_not_awaited()
            self.assertEqual(self.registry.list_service_admins("sound"), [])

        asyncio.run(run())

    def test_management_commands_report_unavailable_registry(self):
        async def run():
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                service_registry=None,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            admin_router = dispatcher.include_router.call_args_list[0].args[0]

            for handler_index, command in (
                (0, "/services"),
                (2, "/service_admin_add sound 12345"),
            ):
                message = self.message(67890, command)
                await admin_router.message.handlers[handler_index].callback(
                    message
                )
                self.assertIn(
                    "реестр настроек служений недоступен",
                    message.answer.await_args.args[0].lower(),
                )

        asyncio.run(run())

    def test_service_settings_response_is_split_to_telegram_safe_chunks(self):
        async def run():
            for index in range(60):
                self.registry.set_responsible_contact(
                    67890,
                    "sound",
                    "Микшер",
                    f"Ответственный {index} " + "И" * 90,
                    10000 + index,
                )
            message = self.message(67890, "/service_settings sound")

            await self.admin_router.message.handlers[1].callback(message)

            self.assertGreater(message.answer.await_count, 1)
            chunks = [
                call.args[0] for call in message.answer.await_args_list
            ]
            self.assertTrue(
                all(len(chunk.encode("utf-16-le")) // 2 <= 4000 for chunk in chunks)
            )
            self.assertIn("Ответственный 59", "".join(chunks))

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
