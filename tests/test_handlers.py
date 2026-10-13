import asyncio
import sqlite3
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
                service_registry=SimpleNamespace(
                    list_services=MagicMock(
                        return_value=[
                            {
                                "id": "assembly",
                                "name": "Собрание",
                                "group_chat_id": -100,
                                "enabled": True,
                            }
                        ]
                    )
                ),
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

            selection = message.answer.await_args.kwargs["reply_markup"]
            button = selection.inline_keyboard[0][0]
            admin_router = dispatcher.include_router.call_args_list[0].args[0]
            service_handler = next(
                item.callback for item in admin_router.callback_query.handlers
                if item.callback.__name__ == "on_service_selected"
            )
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await service_handler(callback)

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
            self.assertIn("Выберите служение", message.answer.await_args.args[0])

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
                service_registry=SimpleNamespace(
                    list_services=MagicMock(
                        return_value=[
                            {
                                "id": "assembly",
                                "name": "Собрание",
                                "group_chat_id": -100,
                                "enabled": True,
                            }
                        ]
                    )
                ),
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

            button = message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            admin_router = dispatcher.include_router.call_args_list[0].args[0]
            service_handler = next(
                item.callback for item in admin_router.callback_query.handlers
                if item.callback.__name__ == "on_service_selected"
            )
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await service_handler(callback)

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

    def test_confirmation_uses_chat_associated_with_original_reminder(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
                "group_chat_id": -1001,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1002,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(return_value={"enabled": True})
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1002,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await router.callback_query.handlers[-1].callback(callback)

            self.assertEqual(
                context.bot.send_message.await_args.args[0], -1001
            )

        asyncio.run(run())

    def test_disabled_assembly_saves_confirmation_without_group_notice(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
                "group_chat_id": -1001,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1001,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(return_value={"enabled": False})
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1001,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            callback = SimpleNamespace(
                data="cfm:2026-10-11:Ответственный",
                from_user=SimpleNamespace(id=12345, username="alex"),
                message=None,
                answer=AsyncMock(),
            )

            await router.callback_query.handlers[-1].callback(callback)

            self.assertTrue(participant["confirmed"])
            self.assertFalse(participant["group_update_sent"])
            context.bot.send_message.assert_not_awaited()
            self.assertIn("служение отключено", callback.answer.await_args.args[0])

        asyncio.run(run())

    def test_failed_confirmation_save_is_reported_and_retried_when_disabled(self):
        async def run():
            participant = {
                "name": "Алексей",
                "tg_id": 12345,
                "confirmed": False,
                "group_update_sent": False,
                "group_chat_id": -1001,
            }
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                state_cache=SimpleNamespace(
                    state={
                        "sunday_date": "2026-10-11",
                        "group_chat_id": -1001,
                        "participants": {"Ответственный": participant},
                    },
                    save=MagicMock(
                        side_effect=[OSError("disk full"), None]
                    ),
                ),
                service_registry=SimpleNamespace(
                    get_service=MagicMock(return_value={"enabled": False})
                ),
                bot=SimpleNamespace(send_message=AsyncMock()),
                group_chat_id=-1001,
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            handler = router.callback_query.handlers[-1].callback

            def make_callback():
                return SimpleNamespace(
                    data="cfm:2026-10-11:Ответственный",
                    from_user=SimpleNamespace(id=12345, username="alex"),
                    message=None,
                    answer=AsyncMock(),
                )

            with patch("bot.handlers._notify_admin", new=AsyncMock()):
                first = make_callback()
                await handler(first)
                self.assertIn("Не удалось сохранить", first.answer.await_args.args[0])
                self.assertTrue(participant["confirmed"])
                self.assertFalse(participant["confirmation_persisted"])

                second = make_callback()
                await handler(second)

            self.assertIn("служение отключено", second.answer.await_args.args[0])
            self.assertNotIn("confirmation_persisted", participant)
            self.assertFalse(participant["group_update_sent"])
            context.bot.send_message.assert_not_awaited()
            self.assertEqual(context.state_cache.save.call_count, 2)

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
            load_service_task_assignments=AsyncMock(
                return_value={"Микшер": ["Имя из расписания"]}
            ),
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
            users_shared=None,
            chat_shared=None,
            answer=AsyncMock(),
        )

    def callback_handler(self, name):
        return next(
            handler.callback
            for handler in self.admin_router.callback_query.handlers
            if handler.callback.__name__ == name
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
                None,
            )

        asyncio.run(run())

    def test_notification_settings_require_guided_flow(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            handler = self.admin_router.message.handlers[6].callback

            manual = self.message(
                12345,
                '/service_notify slides 2 17:30 | Текст группы | {}',
            )
            await handler(manual)
            self.assertIn(
                "пошагово",
                manual.answer.await_args.args[0],
            )
            service = next(
                service for service in self.registry.list_services()
                if service["id"] == "sound"
            )
            self.assertEqual(service["reminder_days"], 1)
            self.assertIsNone(service["group_template"])

        asyncio.run(run())

    def test_service_admin_can_use_guided_notification_setup(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            self.registry.set_notification_settings(
                67890,
                "sound",
                reminder_days=2,
                reminder_time="17:00",
                group_template="Текст группы",
                personal_templates={"Микшер": "Старый текст"},
            )
            message = self.message(12345, "/service_notify")

            await self.admin_router.message.handlers[6].callback(message)

            keyboard = message.answer.await_args.kwargs["reply_markup"]
            buttons = [
                button
                for row in keyboard.inline_keyboard
                for button in row
            ]
            self.assertEqual(
                [(button.text, button.callback_data) for button in buttons],
                [("ЗВУК", "service:service_notify:sound")],
            )

            callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[3].callback(callback)
            self.assertIn(
                "число от 0 до 6",
                callback.message.answer.await_args.args[0],
            )

            staged_input = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_service_parameter"
            )
            async def send_step(text):
                step_message = self.message(12345, text)
                await staged_input(step_message)
                return step_message

            invalid_days = await send_step("7")
            self.assertIn("от 0 до 6", invalid_days.answer.await_args.args[0])
            await send_step("3")
            await send_step("17:15")
            choose_message = await send_step("Новая групповая строка")
            keyboard = choose_message.answer.await_args.kwargs["reply_markup"]
            buttons = [
                button for row in keyboard.inline_keyboard for button in row
            ]
            self.assertIn("Микшер", [button.text for button in buttons])
            template_button = next(button for button in buttons if button.text == "Микшер")
            task_callback = SimpleNamespace(
                data=template_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[1].callback(
                task_callback
            )
            self.assertIn(
                "индивидуальный текст",
                task_callback.message.answer.await_args.args[0],
            )
            choose_finish = await send_step("Текст для микшера {name}")
            finish_button = next(
                button
                for row in choose_finish.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "Завершить и сохранить"
            )
            finish_callback = SimpleNamespace(
                data=finish_button.callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[1].callback(
                finish_callback
            )
            service = self.registry.get_service("sound")
            self.assertEqual(service["reminder_days"], 3)
            self.assertEqual(service["reminder_time"], "17:15")
            self.assertEqual(
                service["group_template"], "Новая групповая строка"
            )
            self.assertEqual(
                service["personal_templates"],
                {"Микшер": "Текст для микшера {name}"},
            )

        asyncio.run(run())

    def test_service_contact_uses_task_choice_and_native_user_picker(self):
        async def run():
            self.registry.set_responsible_contact(
                67890, "sound", "Микшер", "Имя из расписания", 11111
            )
            command = self.message(67890, "/service_contact sound")
            await self.admin_router.message.handlers[5].callback(command)
            task_button = command.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            self.assertEqual(task_button.text, "Микшер")

            task_callback = SimpleNamespace(
                data=task_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            task_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_task_choice"
            )
            await task_handler(task_callback)
            name_markup = task_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            name_button = name_markup.inline_keyboard[0][0]
            self.assertIn(
                "уже привязан Telegram ID 11111",
                task_callback.message.answer.await_args.args[0],
            )
            name_callback = SimpleNamespace(
                data=name_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            name_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_contact_person_choice"
            )
            await name_handler(name_callback)
            user_button = name_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ].keyboard[0][0]
            self.assertIsNotNone(user_button.request_users)

            share_message = self.message(67890, None)
            share_message.users_shared = SimpleNamespace(
                request_id=user_button.request_users.request_id,
                users=[
                    SimpleNamespace(
                        user_id=22222,
                        first_name="Новый",
                        last_name="Пользователь",
                        username="new_user",
                    )
                ],
            )
            user_handler = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_users_shared"
            )
            await user_handler(share_message)
            confirmation = share_message.answer.await_args.kwargs[
                "reply_markup"
            ].inline_keyboard[0][0]
            self.assertIn("Микшер", share_message.answer.await_args.args[0])
            self.assertIn(
                "Имя из расписания",
                share_message.answer.await_args.args[0],
            )

            confirm_callback = SimpleNamespace(
                data=confirmation.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            confirm_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_shared_admin_confirmation"
            )
            await confirm_handler(confirm_callback)
            self.assertEqual(
                self.registry.get_responsible_contact(
                    "sound", "Микшер", "Имя из расписания"
                ),
                {"telegram_id": 22222, "username": "new_user"},
            )

        asyncio.run(run())

    def test_service_contact_links_same_first_name_users_to_schedule_names(self):
        async def run():
            self.context.load_service_task_assignments = AsyncMock(
                return_value={
                    "Микшер": ["Алексей Один", "Алексей Два"],
                }
            )
            task_handler = self.callback_handler("on_task_choice")
            person_handler = self.callback_handler("on_contact_person_choice")
            user_handler = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_users_shared"
            )
            confirm_handler = self.callback_handler(
                "on_shared_admin_confirmation"
            )

            for person_name, user_id, last_name in (
                ("Алексей Один", 30101, "One"),
                ("Алексей Два", 30202, "Two"),
            ):
                command = self.message(67890, "/service_contact sound")
                await self.admin_router.message.handlers[5].callback(command)
                task_button = command.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard[0][0]
                task_callback = SimpleNamespace(
                    data=task_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await task_handler(task_callback)
                person_button = next(
                    button
                    for row in task_callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                    if person_name in button.text
                )
                person_callback = SimpleNamespace(
                    data=person_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await person_handler(person_callback)
                request_id = (
                    person_callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].keyboard[0][0].request_users.request_id
                )
                shared = self.message(67890, None)
                shared.users_shared = SimpleNamespace(
                    request_id=request_id,
                    users=[
                        SimpleNamespace(
                            user_id=user_id,
                            first_name="Алексей",
                            last_name=last_name,
                            username=None,
                        )
                    ],
                )
                await user_handler(shared)
                confirm_button = shared.answer.await_args.kwargs[
                    "reply_markup"
                ].inline_keyboard[0][0]
                confirmation = SimpleNamespace(
                    data=confirm_button.callback_data,
                    from_user=SimpleNamespace(id=67890),
                    message=SimpleNamespace(
                        chat=SimpleNamespace(type="private"),
                        answer=AsyncMock(),
                    ),
                    answer=AsyncMock(),
                )
                await confirm_handler(confirmation)

            contacts = [
                item
                for item in self.registry.list_responsible_contacts(
                    67890, "sound"
                )
                if item["task"] == "Микшер"
            ]
            self.assertEqual(
                {item["name"]: item["telegram_id"] for item in contacts},
                {"Алексей Один": 30101, "Алексей Два": 30202},
            )

        asyncio.run(run())

    def test_large_schedule_task_lists_are_paginated(self):
        async def run():
            assignments = {
                f"Задача {index:02}": [f"Ответственный {index:02}"]
                for index in range(25)
            }
            self.context.load_service_task_assignments = AsyncMock(
                return_value=assignments
            )
            contact_command = self.message(
                67890, "/service_contact sound"
            )
            await self.admin_router.message.handlers[5].callback(
                contact_command
            )
            contact_keyboard = contact_command.answer.await_args_list[-1].kwargs[
                "reply_markup"
            ]
            self.assertEqual(
                len(contact_keyboard.inline_keyboard[0:10]), 10
            )
            self.assertEqual(
                sum(len(row) for row in contact_keyboard.inline_keyboard), 11
            )
            next_page = next(
                button
                for row in contact_keyboard.inline_keyboard
                for button in row
                if button.text == "Далее →"
            )
            page_callback = SimpleNamespace(
                data=next_page.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.callback_handler("on_task_page")(page_callback)
            self.assertEqual(
                sum(
                    len(row)
                    for row in page_callback.message.answer.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                ),
                12,
            )

            notify_command = self.message(67890, "/service_notify sound")
            await self.admin_router.message.handlers[6].callback(
                notify_command
            )
            staged = self.callback_handler("on_service_selected")
            service_callback = SimpleNamespace(
                data="service:service_notify:sound",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await staged(service_callback)
            input_handler = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_service_parameter"
            )
            last_step = None
            for text in ("1", "18:00", "Текст группы"):
                last_step = self.message(67890, text)
                await input_handler(last_step)
            self.assertEqual(
                self.context.load_service_task_assignments.await_args.args,
                ("sound",),
            )
            notify_keyboard = last_step.answer.await_args.kwargs["reply_markup"]
            self.assertEqual(
                sum(len(row) for row in notify_keyboard.inline_keyboard), 12
            )

        asyncio.run(run())

    def test_service_chat_offers_disable_button_instead_of_command_syntax(self):
        async def run():
            self.registry.set_group_chat(
                67890, "sound", -100222, enabled=True
            )
            command = self.message(67890, "/service_chat")
            await self.admin_router.message.handlers[4].callback(command)
            service_button = next(
                button
                for row in command.answer.await_args.kwargs[
                "reply_markup"
                ].inline_keyboard
                for button in row
                if button.text == "ЗВУК"
            )
            service_callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            service_handler = next(
                handler.callback
                for handler in self.admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_service_selected"
            )
            await service_handler(service_callback)
            action_keyboard = service_callback.message.answer.await_args.kwargs[
                "reply_markup"
            ]
            disable_button = next(
                button
                for row in action_keyboard.inline_keyboard
                for button in row
                if button.text == "Отключить служение"
            )
            disable_callback = SimpleNamespace(
                data=disable_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await service_handler(disable_callback)
            sound = self.registry.get_service("sound")
            self.assertFalse(sound["enabled"])
            self.assertEqual(sound["group_chat_id"], -100222)

        asyncio.run(run())

    def test_test_notify_first_selects_service_group_chat(self):
        async def run():
            self.registry.set_group_chat(
                67890, "sound", -100333, enabled=True
            )
            context = SimpleNamespace(
                interaction_logger=MagicMock(),
                debug_logger=MagicMock(),
                admin_id=67890,
                group_chat_id=-100,
                service_registry=self.registry,
                bot=SimpleNamespace(send_message=AsyncMock()),
                prepare_test_previews=AsyncMock(
                    return_value={
                        "sunday_date": date(2026, 10, 18),
                        "group_text": "Предпросмотр группы",
                        "personal_messages": [],
                        "issues": [],
                    }
                ),
            )
            dispatcher = SimpleNamespace(include_router=MagicMock())
            register(dispatcher, context)
            admin_router = dispatcher.include_router.call_args_list[0].args[0]

            test_handler = router.message.handlers[-1].callback
            command = self.message(67890, "/test_notify")
            await test_handler(command)
            selection = command.answer.await_args.kwargs["reply_markup"]
            service_button = next(
                button
                for row in selection.inline_keyboard
                for button in row
                if button.text == "ЗВУК"
            )

            callback = SimpleNamespace(
                data=service_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            service_handler = next(
                handler.callback
                for handler in admin_router.callback_query.handlers
                if handler.callback.__name__ == "on_service_selected"
            )
            await service_handler(callback)

            self.assertEqual(
                context.bot.send_message.await_args_list[0].args[0], -100333
            )
            self.assertIn(
                "Предпросмотр расписания",
                context.bot.send_message.await_args_list[1].args[1],
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

    def test_service_listing_uses_only_readable_russian_labels(self):
        async def run():
            message = self.message(67890, "/services")
            await self.admin_router.message.handlers[0].callback(message)

            result = message.answer.await_args.args[0]
            self.assertIn("СОБРАНИЕ", result)
            self.assertIn("ТРАНСЛЯЦИЯ", result)
            self.assertNotIn("assembly", result)
            self.assertNotIn("broadcast", result)

        asyncio.run(run())

    def test_settings_command_without_arguments_offers_service_buttons(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 12345)
            message = self.message(12345, "/service_settings")

            await self.admin_router.message.handlers[1].callback(message)

            message.answer.assert_awaited_once()
            text = message.answer.await_args.args[0]
            keyboard = message.answer.await_args.kwargs["reply_markup"]
            buttons = [
                button
                for row in keyboard.inline_keyboard
                for button in row
            ]
            self.assertIn("Выберите служение", text)
            self.assertEqual(
                [(button.text, button.callback_data) for button in buttons],
                [("ЗВУК", "service:service_settings:sound")],
            )

            callback = SimpleNamespace(
                data=buttons[0].callback_data,
                from_user=SimpleNamespace(id=12345),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[3].callback(callback)
            self.assertIn(
                "ЗВУК",
                callback.message.answer.await_args.args[0],
            )
            callback.answer.assert_awaited_once()

        asyncio.run(run())

    def test_admin_add_command_without_parameters_guides_user_through_flow(self):
        async def run():
            message = self.message(67890, "/service_admin_add")
            await self.admin_router.message.handlers[2].callback(message)

            keyboard = message.answer.await_args.kwargs["reply_markup"]
            button = keyboard.inline_keyboard[0][0]
            self.assertEqual(button.text, "СОБРАНИЕ")
            callback = SimpleNamespace(
                data=button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[3].callback(callback)
            self.assertIn("выберите пользователя", callback.message.answer.await_args.args[0])
            keyboard = callback.message.answer.await_args.kwargs["reply_markup"]
            request = keyboard.keyboard[0][0].request_users
            self.assertEqual(request.max_quantity, 1)
            self.assertFalse(request.user_is_bot)

            self.assertEqual(self.registry.list_service_admins("assembly"), [])

        asyncio.run(run())

    def test_old_chat_picker_response_is_rejected_after_handler_restart(self):
        async def run():
            first_command = self.message(67890, "/service_chat sound")
            await self.admin_router.message.handlers[4].callback(first_command)
            old_request_id = (
                first_command.answer.await_args.kwargs["reply_markup"]
                .keyboard[0][0].request_chat.request_id
            )

            new_dispatcher = SimpleNamespace(include_router=MagicMock())
            register(new_dispatcher, self.context)
            new_admin_router = (
                new_dispatcher.include_router.call_args_list[0].args[0]
            )
            new_command = self.message(67890, "/service_chat coffee")
            await new_admin_router.message.handlers[4].callback(new_command)
            new_request_id = (
                new_command.answer.await_args.kwargs["reply_markup"]
                .keyboard[0][0].request_chat.request_id
            )
            self.assertNotEqual(old_request_id, new_request_id)

            stale_response = self.message(67890, None)
            stale_response.chat_shared = SimpleNamespace(
                request_id=old_request_id,
                chat_id=-1009911,
                title="Старый выбор",
                username=None,
            )
            await new_admin_router.message.handlers[10].callback(stale_response)

            self.assertIn("не соответствует активному запросу", stale_response.answer.await_args.args[0])
            self.assertIsNone(self.registry.get_service("sound")["group_chat_id"])
            self.assertIsNone(self.registry.get_service("coffee")["group_chat_id"])

        asyncio.run(run())

    def test_staged_input_filter_does_not_consume_commands(self):
        async def run():
            handler = next(
                handler
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_service_parameter"
            )
            filter_object = handler.filters[0]

            command_matches = await filter_object.call(
                value=self.message(67890, "/test_notify")
            )
            text_matches = await filter_object.call(
                value=self.message(67890, "some staged input")
            )

            self.assertFalse(command_matches)
            self.assertTrue(text_matches)

        asyncio.run(run())

    def test_switching_management_commands_clears_old_staged_input(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            remove = self.message(67890, "/service_admin_remove sound")
            await self.admin_router.message.handlers[3].callback(remove)
            settings = self.message(67890, "/service_settings sound")
            await self.admin_router.message.handlers[1].callback(settings)
            staged_input = next(
                handler.callback
                for handler in self.admin_router.message.handlers
                if handler.callback.__name__ == "on_service_parameter"
            )
            numeric_text = self.message(67890, "24680")

            await staged_input(numeric_text)

            self.assertEqual(self.registry.list_service_admins("sound"), [24680])
            numeric_text.answer.assert_not_awaited()

        asyncio.run(run())

    def test_back_to_services_reports_registry_read_errors(self):
        async def run():
            self.context.service_registry = SimpleNamespace(
                is_super_admin=MagicMock(
                    side_effect=sqlite3.OperationalError("database unavailable")
                )
            )
            callback = SimpleNamespace(
                data="service:back_to_services:list",
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )

            await self.admin_router.callback_query.handlers[3].callback(callback)

            self.assertEqual(
                callback.message.answer.await_args.args[0],
                "Не удалось прочитать реестр служений.",
            )
            callback.answer.assert_awaited_once()
            self.context.debug_logger.exception.assert_called()

        asyncio.run(run())

    def test_shared_user_is_confirmed_before_becoming_service_admin(self):
        async def run():
            command = self.message(67890, "/service_admin_add")
            await self.admin_router.message.handlers[2].callback(command)
            service_keyboard = command.answer.await_args.kwargs["reply_markup"]
            assembly_button = service_keyboard.inline_keyboard[0][0]
            selected_service_message = self.message(
                67890, "service selection", chat_type="private"
            )
            service_callback = SimpleNamespace(
                data=assembly_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=selected_service_message,
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[3].callback(
                service_callback
            )

            user_keyboard = selected_service_message.answer.await_args.kwargs[
                "reply_markup"
            ]
            request_id = user_keyboard.keyboard[0][0].request_users.request_id
            share_message = self.message(67890, None)
            share_message.users_shared = SimpleNamespace(
                request_id=request_id,
                users=[
                    SimpleNamespace(
                        user_id=24680,
                        first_name="Анна",
                        username="candidate",
                    )
                ],
            )
            await self.admin_router.message.handlers[9].callback(
                share_message
            )
            self.assertEqual(self.registry.list_service_admins("assembly"), [])
            confirmation = share_message.answer.await_args.kwargs["reply_markup"]
            confirm_button = confirmation.inline_keyboard[0][0]
            self.assertIn("Анна", share_message.answer.await_args.args[0])
            self.assertIn("@candidate", share_message.answer.await_args.args[0])

            admin_callback = SimpleNamespace(
                data=confirm_button.callback_data,
                from_user=SimpleNamespace(id=67890),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[4].callback(
                admin_callback
            )
            self.assertEqual(
                self.registry.list_service_admins("assembly"), [24680]
            )
            self.assertIn(
                "24680",
                admin_callback.message.answer.await_args.args[0],
            )

        asyncio.run(run())

    def test_shared_group_chat_is_saved_and_service_can_be_disabled(self):
        async def run():
            command = self.message(67890, "/service_chat sound")
            await self.admin_router.message.handlers[4].callback(command)
            keyboard = command.answer.await_args.kwargs["reply_markup"]
            request = keyboard.keyboard[0][0].request_chat
            self.assertFalse(request.chat_is_channel)
            self.assertTrue(request.bot_is_member)

            shared_chat = self.message(67890, None)
            shared_chat.chat_shared = SimpleNamespace(
                request_id=request.request_id,
                chat_id=-1002233,
                title="Звук",
                username=None,
            )
            await self.admin_router.message.handlers[10].callback(shared_chat)
            sound = self.registry.get_service("sound")
            self.assertEqual(sound["group_chat_id"], -1002233)
            self.assertTrue(sound["enabled"])
            self.assertIn("-1002233", shared_chat.answer.await_args.args[0])

            disable = self.message(67890, "/service_chat sound off")
            await self.admin_router.message.handlers[4].callback(disable)
            sound = self.registry.get_service("sound")
            self.assertEqual(sound["group_chat_id"], -1002233)
            self.assertFalse(sound["enabled"])

        asyncio.run(run())

    def test_settings_message_has_next_action_button(self):
        async def run():
            self.registry.add_service_admin(67890, "sound", 24680)
            selection_message = self.message(
                67890, "/service_settings sound"
            )
            await self.admin_router.message.handlers[1].callback(
                selection_message
            )
            keyboard = selection_message.answer.await_args.kwargs["reply_markup"]
            edit_button = keyboard.inline_keyboard[0][0]
            self.assertEqual(
                edit_button.callback_data, "service:edit_notify:sound"
            )
            self.assertIn(
                "изменить дни",
                selection_message.answer.await_args.args[0].lower(),
            )

            callback = SimpleNamespace(
                data=edit_button.callback_data,
                from_user=SimpleNamespace(id=24680),
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="private"),
                    answer=AsyncMock(),
                ),
                answer=AsyncMock(),
            )
            await self.admin_router.callback_query.handlers[3].callback(callback)
            self.assertIn(
                "число от 0 до 6",
                callback.message.answer.await_args.args[0],
            )

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
