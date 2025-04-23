import asyncio
import os
import yaml
import logging
import traceback
from datetime import datetime, timedelta, timezone, time
from typing import Dict, List, Optional, Any, Union, Tuple
import strip_markdown
from dotenv import load_dotenv

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from cachetools import TTLCache

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot, BotCommand, BotCommandScopeDefault, \
    BotCommandScopeChat
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler
)
from deepseek_api import DeepSeekAPI
from yandexgpt_api import YandexGptAPI
from database import Database
from config import *
from utils import *

# Настройка логирования
logging.basicConfig(
    filename="main.log",
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Создаем отдельный обработчик для вывода логов в консоль
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
console_handler.setFormatter(console_formatter)

logger = logging.getLogger(__name__)
logger.addHandler(console_handler)

# Загрузка переменных окружения
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
YC_FOLDER_ID = os.getenv("YC_FOLDER_ID")
YC_SECRET_ID = os.getenv("YC_SECRET_ID")

if not BOT_TOKEN or not YC_FOLDER_ID or not YC_SECRET_ID:
    logger.error("Не указаны обязательные переменные окружения")
    raise EnvironmentError("Отсутствуют обязательные переменные окружения")


class ReminderBot:
    """Бот для управления напоминаниями с использованием LLM."""

    def __init__(self) -> None:
        """Инициализация бота и его зависимостей."""
        self.db = Database()
        self.deepseek = DeepSeekAPI()
        self.yandexgpt = YandexGptAPI(YC_FOLDER_ID, YC_SECRET_ID)
        self.scheduler = AsyncIOScheduler()
        self.db_tasks_listing_page = 0
        self.bot = Bot(token=BOT_TOKEN)
        # Добавляем кэш для часто запрашиваемых данных (TTL = 5 минут)
        self.chat_cache = TTLCache(maxsize=100, ttl=300)
        self.last_log_position = 0  # Для оптимизации чтения лога
        logger.info("ReminderBot initialized")

    def user_is_admin(self, user: Dict[str, Any]) -> bool:
        """Проверяет, является ли пользователь администратором.

        Args:
            user: Словарь с данными пользователя

        Returns:
            True, если пользователь администратор, иначе False
        """
        return user.get("is_admin", False) or user.get("telegram_id", 0) == ADMIN_ID

    def is_chat_allowed(self, chat_id: int, user_id: int = None) -> bool:
        """Проверяет, разрешено ли чату использовать бота.

        Args:
            chat_id: ID чата
            user_id: ID пользователя (для приватных чатов)

        Returns:
            True, если чату разрешен доступ, иначе False
        """
        # Сначала проверяем кэш
        cache_key = f"chat_{chat_id}"
        if cache_key in self.chat_cache:
            return self.chat_cache[cache_key]

        chat = self.db.get_chat(chat_id)

        # Регистрация чата, если он новый
        if chat is None:
            logger.info(f"Регистрация нового чата: {chat_id}")
            # Если это приватный чат, его ID совпадает с ID пользователя
            is_group = chat_id < 0  # В Telegram ID групп отрицательные
            self.db.create_chat({
                'chat_id': chat_id,
                'is_group': is_group,
                'is_allowed': chat_id in ALLOWED_USERS or (user_id and user_id in ALLOWED_USERS)
            })
            chat = self.db.get_chat(chat_id)

        is_allowed = chat is not None and (chat.get("is_allowed", False) or chat_id in ALLOWED_USERS)
        # Сохраняем результат в кэше
        self.chat_cache[cache_key] = is_allowed

        return is_allowed

    def select_nearest_time_for_tag(self, chat_id: int, tag_name: str) -> datetime:
        """Вычисляет оптимальное время для нового напоминания.

        Args:
            chat_id: ID чата
            tag_name: Название тега

        Returns:
            Рекомендуемое время для нового напоминания
        """
        for tag in self.db.get_chat_tags(chat_id):
            if tag["name"] == tag_name:
                tasks = self.db.list_reminders_by_tag(chat_id, tag["id"])
                tasks_timestamps = [parse_timestamp(task["due_time"]) for task in tasks]
                tasks_timestamps.sort()

                for i in range(len(tasks_timestamps) - 1):
                    if tasks_timestamps[i] <= datetime.now(SERVER_TIMEZONE):
                        continue
                    if tasks_timestamps[i + 1] - tasks_timestamps[i] > OPTIMAL_TASKS_DELTA:
                        return tasks_timestamps[i] + OPTIMAL_TASKS_DELTA

        return datetime.now(SERVER_TIMEZONE) + OPTIMAL_TASKS_DELTA

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду /start.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка доступа от неразрешенного чата: {chat_id}")
            await update.message.reply_text(
                "Извините, у вас нет доступа к этому боту. Обратитесь к администратору."
            )
            return

        keyboard = [
            [InlineKeyboardButton("Мои напоминания", callback_data='list_tasks')],
            [InlineKeyboardButton("Мои теги", callback_data='list_tags')],
            [InlineKeyboardButton("Помощь", callback_data='help')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await update.message.reply_text(
                f"Добро пожаловать! Этот бот поможет {'вашей группе' if chat_id < 0 else 'вам'} управлять напоминаниями. Выберите действие:",
                reply_markup=reply_markup
            )
            logger.info(f"Чат {chat_id} запустил бота")
        except Exception as e:
            logger.error(f"Ошибка при отправке приветствия: {e}")

    async def ask_llm_extract(self, tags: List[Dict], query: str) -> Dict:
        """Обращается к LLM для извлечения задач из запроса пользователя.

        Args:
            tags: Список тегов пользователя
            query: Текст запроса пользователя

        Returns:
            Словарь с извлеченными задачами

        Raises:
            Exception: При ошибке взаимодействия с LLM или обработке ответа
        """
        tag_str = ", ".join([f"{t['name']}" for t in tags])
        response_format = '{"tagName": [{"text": "taskTitle"}]}'
        system = (
            f"Ты - умный ассистент пользователя, помогающий ему распланировать напоминания. "
            f"Извлеки список задач из сообщения пользователя и распредели их по тегам. "
            f"Учти пожелания пользователя по их количеству и привязанности к тегам. "
            f"Список тегов: [{tag_str}]. "
            f"В ответе предоставь только валидный JSON в формате {response_format} без пояснений. "
            f"Если пользователь хочет несколько напоминаний, продублируй их в возвращаемом списке "
            f"столько раз, сколько он просит, но не больше тридцати."
        )

        logger.info(f"LLM extract query: {query}")
        try:
            # Добавляем повторную попытку для повышения надежности
            for attempt in range(3):
                try:
                    response = await self.yandexgpt.query(system, query)
                    logger.info(f"LLM extract response received")
                    return yaml.safe_load(strip_markdown.strip_markdown(response).strip("`"))
                except Exception as e:
                    if attempt < 2:
                        logger.warning(
                            f"Попытка {attempt + 1} извлечения задач из LLM не удалась: {e}. Повторная попытка...")
                        await asyncio.sleep(1)
                    else:
                        raise
        except Exception as e:
            logger.error(f"Ошибка при извлечении задач из LLM: {e}")
            raise

    async def ask_llm_plan(self, tags: List[Dict], tasks: Union[Dict, str, Any], query: str) -> Dict:
        """Обращается к LLM для планирования времени извлеченных задач.

        Args:
            tags: Список тегов пользователя
            tasks: Словарь извлеченных задач или другой объект
            query: Исходный запрос пользователя

        Returns:
            Словарь с распланированными задачами

        Raises:
            Exception: При ошибке взаимодействия с LLM или обработке ответа
        """
        tag_str = ", ".join([f"{t['name']} ({t['start_time']}-{t['end_time']})" for t in tags])
        response_format = '{"tagName": [{"text": "taskTitle", "time": "DT_FORMAT"}]}'.replace("DT_FORMAT", DT_FORMAT)
        current_weekday = WEEKDAYS[datetime.today().weekday()]

        system = (
            f"Ты - умный ассистент пользователя, помогающий ему распланировать напоминания. "
            f"Проставь всем извлечённым задачам из сообщения пользователя время как можно ближе "
            f"к настоящему, но не раньше текущего времени {datetime.now(SERVER_TIMEZONE).strftime(DT_FORMAT)} "
            f"({current_weekday}). По умолчанию считай, что напомнить нужно сегодня, если это позволяет "
            f"окно планирования тега и не сказано обратное в сообщении пользователя. "
            f"Учитывай пожелания пользователя, держи адекватное количество времени между задачами, "
            f"а также предпочитай планировать днём, а не ночью (если это не попросил пользователь). "
            f"Список тегов и окон планирования каждого из них: [{tag_str}]. "
            f"На вход дается JSON с двумя полями: extracted_tasks - извлеченные задачи с разбивкой "
            f"по тегам, которым нужно выставить время; user_query - запрос пользователя, пожелания "
            f"из которого нужно учесть. В ответе предоставь только валидный JSON в формате {response_format} "
            f"без пояснений, datetime строго в формате {DT_FORMAT}"
        )

        logger.info(f"LLM plan for tasks")

        # Безопасное создание словаря запроса в зависимости от типа tasks
        if isinstance(tasks, dict):
            extracted_tasks = tasks.copy()
        else:
            # Если tasks не словарь, преобразуем его в строку
            extracted_tasks = str(tasks)

        tasks_with_query = {"extracted_tasks": extracted_tasks, "user_query": query}

        try:
            # Добавляем повторную попытку для повышения надежности
            for attempt in range(3):
                try:
                    response = await self.yandexgpt.query(system, str(tasks_with_query))
                    logger.info(f"LLM plan response received")
                    return yaml.safe_load(strip_markdown.strip_markdown(response).strip("`"))
                except Exception as e:
                    if attempt < 2:
                        logger.warning(
                            f"Попытка {attempt + 1} планирования задач через LLM не удалась: {e}. Повторная попытка...")
                        await asyncio.sleep(1)
                    else:
                        raise
        except Exception as e:
            logger.error(f"Ошибка при планировании задач через LLM: {e}")
            raise

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает текстовые сообщения пользователя.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Сообщение от неразрешенного чата: {chat_id}")
            return

        # Проверяем, не слишком ли короткое сообщение
        query = update.message.text.replace("\n", ";")
        if len(query.strip()) < 3:
            await update.message.reply_text(
                "Пожалуйста, введите более подробное описание задачи или напоминания."
            )
            return

        # Отправляем индикатор печати
        await self.bot.send_chat_action(chat_id=chat_id, action=telegram.constants.ChatAction.TYPING)

        # Получаем теги чата
        tags = self.db.get_chat_tags(chat_id) + [{"name": "default", "start_time": "00:00", "end_time": "23:59"}]

        try:
            # Извлекаем задачи из сообщения
            tasks_without_time = await self.ask_llm_extract(tags, query)

            # Планируем время для задач
            tasks = await self.ask_llm_plan(tags, tasks_without_time, query)
            context.user_data['pending_tasks'] = tasks

            # Сохраняем неподтвержденные напоминания
            created_count = 0
            for tag, items in tasks.items():
                for task in items:
                    # Сохранение в БД
                    due_time = parse_datetime(task["time"])
                    if due_time:
                        self.db.create_unconfirmed_reminder(
                            chat_id=chat_id,
                            created_by=user.id,
                            text=task['text'],
                            tag_id=tag,
                            due_time=due_time
                        )
                        created_count += 1
                    else:
                        logger.warning(f"Не удалось распарсить время '{task['time']}' для задачи '{task['text']}'")

            # Формируем клавиатуру для подтверждения задач
            keyboard = []
            unconfirmed_reminders = self.db.list_unconfirmed_reminders(chat_id)

            # Группируем задачи по дате для более удобного отображения
            grouped_tasks = self._group_unconfirmed_tasks_by_date(unconfirmed_reminders)

            for date_group, tasks in grouped_tasks.items():
                keyboard.append(
                    [InlineKeyboardButton(f"📅 {date_group}", callback_data="ignore")])  # Use ignore to bypass

                for task in tasks:
                    due_time = parse_timestamp(task['due_time'])
                    time_str = due_time.strftime('%H:%M')
                    text = f"{time_str} - {task['text']} [{task['tag_id']}]"
                    callback_data = f"confirm_task:{task['id']}"
                    keyboard.append([InlineKeyboardButton(text, callback_data=callback_data)])

            keyboard.append([InlineKeyboardButton("Отменить оставшиеся", callback_data="confirm_task:remove")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            if created_count > 0:
                await update.message.reply_text(
                    "Выберите задачи для добавления:",
                    reply_markup=reply_markup
                )
            else:
                await update.message.reply_text(
                    "Не удалось создать напоминания из вашего сообщения. Попробуйте перефразировать."
                )

            logger.info(f"Создано {created_count} неподтвержденных напоминаний для чата {chat_id}")

        except Exception as e:
            logger.error(f"Ошибка при обработке сообщения: {e}\n{traceback.format_exc()}")
            await update.message.reply_text(
                "Произошла ошибка при обработке вашего запроса. Пожалуйста, попробуйте еще раз."
            )

    def _group_unconfirmed_tasks_by_date(self, tasks: List[Dict]) -> Dict[str, List[Dict]]:
        """Группирует неподтвержденные задачи по дате.

        Args:
            tasks: Список неподтвержденных задач

        Returns:
            Словарь с задачами, сгруппированными по датам
        """
        grouped = {}

        for task in tasks:
            due_time = parse_timestamp(task['due_time'])
            today = datetime.now(SERVER_TIMEZONE).date()
            tomorrow = today + timedelta(days=1)

            if due_time.date() == today:
                date_group = "Сегодня"
            elif due_time.date() == tomorrow:
                date_group = "Завтра"
            else:
                date_group = format_date(due_time)

            if date_group not in grouped:
                grouped[date_group] = []

            grouped[date_group].append(task)

        # Сортируем задачи внутри каждой группы по времени
        for date_group in grouped:
            grouped[date_group].sort(key=lambda x: parse_timestamp(x['due_time']))

        # Возвращаем словарь с отсортированными ключами
        return {k: grouped[k] for k in sorted(grouped.keys())}

    async def confirm_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает подтверждение задачи пользователем."""
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка подтверждения задачи от неразрешенного чата: {chat_id}")
            return

        query = update.callback_query

        # Пропускаем обработку заголовков дат
        if query.data == "date_header":
            await self.bot.answer_callback_query(query.id)
            return

        unconfirmed_task_id = query.data.split(":")[1]

        try:
            if unconfirmed_task_id == "remove":
                deleted_count = self.db.delete_unconfirmed_reminders(chat_id)
                logger.info(f"Удалено {deleted_count} неподтвержденных напоминаний для чата {chat_id}")
                await self.bot.answer_callback_query(query.id, text=f"Отменено {deleted_count} напоминаний")

                # Обновляем сообщение, чтобы удалить кнопки
                if query.message:
                    await query.message.edit_text(f"Отменено {deleted_count} напоминаний")
                return

            task_data = self.db.get_unconfirmed_reminder(unconfirmed_task_id)
            if not task_data:
                logger.warning(f"Попытка подтвердить несуществующее напоминание: {unconfirmed_task_id}")
                await self.bot.answer_callback_query(query.id, text="Напоминание не найдено")
                return

            # Сохранение в БД
            reminder_id = self.db.create_reminder(
                chat_id=chat_id,
                created_by=user.id,
                text=task_data['text'],
                tag_id=task_data['tag_id'],
                due_time=parse_timestamp(task_data['due_time'])
            )

            self.db.delete_unconfirmed_reminder(unconfirmed_task_id)

            if reminder_id:
                # Обновление клавиатуры в интерфейсе - создаем новую клавиатуру
                if query.message and query.message.reply_markup:
                    new_keyboard = []
                    for row in query.message.reply_markup.inline_keyboard:
                        new_row = []
                        for button in row:
                            if button.callback_data == query.data:
                                # Создаем новую кнопку с измененным текстом
                                new_row.append(InlineKeyboardButton(
                                    f"✅ {button.text}",
                                    callback_data=button.callback_data
                                ))
                            else:
                                new_row.append(button)
                        new_keyboard.append(new_row)

                    try:
                        await query.message.edit_reply_markup(InlineKeyboardMarkup(new_keyboard))
                    except Exception as e:
                        logger.error(f"Не удалось обновить клавиатуру: {e}")

                await self.bot.send_message(
                    chat_id,
                    f"✅ Задача «{task_data['text']}» добавлена на {short_format_datetime(parse_timestamp(task_data['due_time']))}!"
                )
                logger.info(f"Пользователь {user.id} подтвердил задачу '{task_data['text']}' в чате {chat_id}")
            else:
                await self.bot.send_message(chat_id, "❌ Не удалось сохранить задачу")

            await self.bot.answer_callback_query(query.id)

        except Exception as e:
            logger.error(f"Ошибка при подтверждении задачи: {e}")
            await self.bot.answer_callback_query(query.id, text="Произошла ошибка")

    async def ignore(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает нулевой колбэк."""
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка нулевого колбэка от неразрешенного чата: {chat_id}")
            return

        query = update.callback_query

        try:
            await self.bot.answer_callback_query(query.id)
            return
        except Exception as e:
            logger.error(f"Ошибка при подтверждении задачи: {e}")
            await self.bot.answer_callback_query(query.id, text="Произошла ошибка")

    async def reschedule_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на перенос напоминания."""
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            return

        query = update.callback_query
        parts = query.data.split(":")
        task_id = parts[1]
        reschedule_delta = parts[2]

        try:
            # Определяем величину переноса
            delta = self._get_reschedule_delta(reschedule_delta)
            task_data = self.db.get_reminder(task_id)

            if not task_data:
                await self.bot.answer_callback_query(query.id, text="Напоминание не найдено")
                return

            new_due_dt = datetime.now(SERVER_TIMEZONE) + delta

            if self.db.reschedule(task_id, new_due_dt):
                # Открепляем текущее сообщение, так как задача перенесена
                try:
                    message_id = task_data.get('message_id')
                    if message_id:
                        await self.bot.unpin_chat_message(
                            chat_id=chat_id,
                            message_id=message_id
                        )
                        logger.info(f"Откреплено перенесенное напоминание {task_id}")
                except Exception as e:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text="❌ ОШИБКА: не удалось открепить сообщение. Пожалуйста, открепите его самостоятельно чтобы не запутаться ❗",
                    )
                    logger.error(f"Не удалось открепить сообщение при переносе: {e}")

                # Обновляем сообщение с информацией о переносе
                if query.message:
                    try:
                        new_text = f"{query.message.text}\n\n⏰ Перенесено на {short_format_datetime(new_due_dt)}"
                        await query.message.edit_text(new_text)
                    except Exception as e:
                        logger.error(f"Не удалось обновить сообщение: {e}")

                await self.bot.send_message(
                    chat_id,
                    f"⏰ Задача «{task_data['text']}» перенесена на {short_format_datetime(new_due_dt)}!"
                )
                logger.info(f"Пользователь {user.id} перенес задачу на {new_due_dt}")
            else:
                await self.bot.send_message(chat_id, "❌ Не удалось перенести задачу")

            await self.bot.answer_callback_query(query.id)

        except Exception as e:
            logger.error(f"Ошибка при переносе задачи: {e}")
            await self.bot.answer_callback_query(query.id, text="Произошла ошибка")

    def _get_reschedule_delta(self, reschedule_type: str) -> timedelta:
        """Определяет интервал для переноса напоминания.

        Args:
            reschedule_type: Тип переноса

        Returns:
            Временной интервал для переноса
        """
        deltas = {
            "hour": timedelta(hours=1),
            "8hours": timedelta(hours=8),
            "day": timedelta(days=1),
            "2days": timedelta(days=2),
            "week": timedelta(weeks=1),
            "month": timedelta(days=31),
            "3months": timedelta(days=93)
        }

        if reschedule_type in deltas:
            return deltas[reschedule_type]

        if reschedule_type == "evening":
            delta = timedelta(minutes=30)
            while (datetime.now(SERVER_TIMEZONE) + delta).hour <= 19:
                delta += timedelta(hours=1)
            return delta

        if reschedule_type == "weekends":
            delta = timedelta(days=1)
            while (datetime.now(SERVER_TIMEZONE) + delta).weekday() <= 4:
                delta += timedelta(days=1)
            return delta

        # Значение по умолчанию
        return timedelta(minutes=30)

    async def check_reminders(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Проверяет и отправляет напоминания, для которых наступило время."""
        dt = datetime.now(SERVER_TIMEZONE)
        try:
            reminders = self.db.get_due_reminders(dt)
            logger.info(f"Проверка напоминаний: найдено {len(reminders)} активных напоминаний")

            # Группируем напоминания по чатам для оптимизации
            chat_reminders = {}
            for reminder in reminders:
                chat_id = reminder['chat_id']
                if chat_id not in chat_reminders:
                    chat_reminders[chat_id] = []
                chat_reminders[chat_id].append(reminder)

            for chat_id, chat_reminder_list in chat_reminders.items():
                try:
                    chat = self.db.get_chat(chat_id)
                    if not chat:
                        logger.error(f"Чат {chat_id} не найден")
                        continue

                    for reminder in chat_reminder_list:
                        reminder_time = parse_timestamp(reminder["due_time"])

                        # Проверка, не отправляем напоминание раньше времени
                        if reminder_time > dt:
                            continue

                        try:
                            # Подготовка сообщения с рекомендациями
                            assist = ""
                            if reminder.get("assist") and reminder["assist"].strip():
                                assist = f"\n\n---\n{reminder['assist']}"

                            # Создание клавиатуры для отложенных напоминаний
                            keyboard = self._create_reschedule_keyboard(reminder["id"])
                            reply_markup = InlineKeyboardMarkup(keyboard)

                            # Отправка напоминания в чат
                            reminder_message = f"⏰ *Напоминание:* {reminder['text']}{assist}"
                            message = await self.bot.send_message(
                                chat_id=chat_id,
                                text=reminder_message,
                                reply_markup=reply_markup,
                                parse_mode='Markdown'
                            )

                            # Закрепляем сообщение с напоминанием
                            try:
                                await self.bot.pin_chat_message(
                                    chat_id=chat_id,
                                    message_id=message.message_id,
                                    disable_notification=False
                                )
                                # Сохраняем ID сообщения в БД для будущей открепки
                                self.db.update_reminder_message_id(reminder['id'], message.message_id)
                                logger.info(f"Закреплено напоминание {reminder['id']} в чате {chat_id}")
                            except Exception as e:
                                await self.bot.send_message(
                                    chat_id=chat_id,
                                    text="❌ ОШИБКА: не удалось закрепить сообщение. Пожалуйста, закрепите его самостоятельно чтобы не потерять ❗",
                                )
                                logger.error(f"Не удалось закрепить сообщение: {e}")

                            # Отмечаем напоминание как отправленное
                            self.db.mark_reminder_completed(reminder['id'])
                            logger.info(f"Отправлено напоминание {reminder['id']} в чат {chat_id}")

                        except Exception as e:
                            logger.error(f"Ошибка при отправке напоминания {reminder['id']}: {e}")

                except Exception as e:
                    logger.error(f"Ошибка при обработке напоминаний чата {chat_id}: {e}")

        except Exception as e:
            logger.error(f"Ошибка при проверке напоминаний: {e}")

    def _create_reschedule_keyboard(self, reminder_id: int) -> List[List[InlineKeyboardButton]]:
        """Создает клавиатуру для переноса напоминания.

        Args:
            reminder_id: ID напоминания

        Returns:
            Кнопки для клавиатуры
        """
        return [
            [
                InlineKeyboardButton("через час", callback_data=f"reschedule_task:{reminder_id}:hour"),
                InlineKeyboardButton("через день", callback_data=f"reschedule_task:{reminder_id}:day"),
                InlineKeyboardButton("через неделю", callback_data=f"reschedule_task:{reminder_id}:week"),
            ],
            [
                InlineKeyboardButton("через 8 часов", callback_data=f"reschedule_task:{reminder_id}:8hours"),
                InlineKeyboardButton("через 2 дня", callback_data=f"reschedule_task:{reminder_id}:2days"),
                InlineKeyboardButton("через месяц", callback_data=f"reschedule_task:{reminder_id}:month"),
            ],
            [
                InlineKeyboardButton("через 3 месяца", callback_data=f"reschedule_task:{reminder_id}:3months"),
                InlineKeyboardButton("вечером", callback_data=f"reschedule_task:{reminder_id}:evening"),
                InlineKeyboardButton("в выходные", callback_data=f"reschedule_task:{reminder_id}:weekends"),
            ],
            [
                InlineKeyboardButton("✓ Выполнено", callback_data=f"complete_task:{reminder_id}")
            ]
        ]

    async def complete_task(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает отметку задачи как выполненной."""
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            return

        query = update.callback_query
        task_id = query.data.split(":")[1]

        try:
            task_data = self.db.get_reminder(task_id)
            if not task_data:
                await self.bot.answer_callback_query(query.id, text="Напоминание не найдено")
                return

            if self.db.mark_reminder_completed(task_id):
                # Обновляем сообщение
                if query.message:
                    try:
                        new_text = f"{query.message.text}\n\n✅ Выполнено!"
                        await query.message.edit_text(new_text)
                    except Exception as e:
                        logger.error(f"Не удалось обновить сообщение: {e}")

                # Открепляем сообщение
                try:
                    # Проверяем, есть ли ID сообщения в данных задачи
                    message_id = task_data.get('message_id')

                    if message_id:
                        # Открепляем сообщение
                        await self.bot.unpin_chat_message(
                            chat_id=chat_id,
                            message_id=message_id
                        )
                        logger.info(f"Откреплено сообщение с напоминанием {task_id}")
                except Exception as e:
                    await self.bot.send_message(
                        chat_id=chat_id,
                        text="❌ ОШИБКА: не удалось открепить сообщение. Пожалуйста, открепите его самостоятельно чтобы не запутаться ❗",
                    )
                    logger.error(f"Не удалось открепить сообщение: {e}")

                await self.bot.answer_callback_query(query.id, text="Задача отмечена как выполненная")
                logger.info(f"Пользователь {user.id} отметил задачу '{task_data['text']}' как выполненную")
            else:
                await self.bot.answer_callback_query(query.id, text="Не удалось обновить статус задачи")

        except Exception as e:
            logger.error(f"Ошибка при отметке задачи как выполненной: {e}")
            await self.bot.answer_callback_query(query.id, text="Произошла ошибка")

    async def ask_llm_assist(self, query: str) -> Dict:
        """Запрашивает у LLM дополнительные рекомендации для задачи.

        Args:
            query: Текст задачи

        Returns:
            Словарь с рекомендациями

        Raises:
            Exception: При ошибке взаимодействия с LLM или обработке ответа
        """
        response_format = '{"hasAssist": bool, "assist": "text"}'
        system = (
            f"Ты - умный ассистент пользователя, помогающий ему выполнять свои задачи. "
            f"Подумай, какая информация может помочь пользователю выполнить задачу и составь "
            f"небольшой текст размером в один параграф с конкретными пунктами-советами и "
            f"небольшим вступлением, чтобы пользователь не испугался, а понял, что ты помогаешь. "
            f"Разделяй советы новой строкой. Учитывай, что пользователь и сам бы справился с задачей, "
            f"он умный и знает что делать, но действительно полезный совет не помешал бы ему. "
            f"Будь вежливым и дружелюбным. Если задача слишком простая и супер интересных советов нет - "
            f"вместо этого просто подбодри его, но не объясняй очевидные вещи. "
            f"На вход дается текст задачи пользователя. В ответе предоставь только валидный JSON "
            f"в формате {response_format} без пояснений"
        )

        logger.info(f"Запрос советов LLM для задачи: {query}")
        try:
            for attempt in range(3):
                try:
                    response = await self.yandexgpt.query(system, str(query))
                    logger.info(f"Получен ответ от LLM с советами")
                    return yaml.safe_load(strip_markdown.strip_markdown(response).strip("`"))
                except Exception as e:
                    if attempt < 2:
                        logger.warning(f"Попытка {attempt + 1} получения советов не удалась: {e}. Повторная попытка...")
                        await asyncio.sleep(1)
                    else:
                        raise
        except Exception as e:
            logger.error(f"Ошибка при получении советов LLM: {e}")
            return {"hasAssist": False, "assist": ""}

    async def assist(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Создает рекомендации для предстоящих напоминаний.

        Args:
            context: Контекст планировщика
        """
        dt = datetime.now(SERVER_TIMEZONE) + timedelta(hours=5)
        try:
            reminders = self.db.get_due_reminders(dt)
            processed = 0

            for reminder in reminders:
                # Пропускаем напоминания, для которых уже есть рекомендации
                if reminder.get("assist") is not None and reminder["assist"].strip():
                    continue

                try:
                    # Получаем рекомендации от LLM
                    assist = await self.ask_llm_assist(reminder["text"])
                    if not isinstance(assist, dict) or not assist.get("hasAssist", False) or not assist.get("assist"):
                        continue

                    # Сохраняем рекомендации в БД
                    self.db.update_task_assist(reminder["id"], assist["assist"])
                    processed += 1

                except Exception as e:
                    logger.error(f"Ошибка при создании рекомендаций для напоминания {reminder['id']}: {e}")

            if processed > 0:
                logger.info(f"Создано рекомендаций для {processed} напоминаний")

        except Exception as e:
            logger.error(f"Ошибка в планировщике рекомендаций: {e}")

    async def daily(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Отправляет ежедневное расписание задач.

        Args:
            context: Контекст планировщика
        """
        dt = datetime.now(SERVER_TIMEZONE).replace(hour=23, minute=59, second=59)
        logger.info("Запуск ежедневного уведомления")

        try:
            reminders = self.db.get_due_reminders(dt)
            reminders_per_chat = {}

            # Группируем напоминания по чатам
            for reminder in reminders:
                chat_id = reminder["chat_id"]
                if chat_id not in reminders_per_chat:
                    reminders_per_chat[chat_id] = []
                reminders_per_chat[chat_id].append(reminder)

            # Отправляем сообщения в каждый чат
            for chat_id, chat_reminders in reminders_per_chat.items():
                if not chat_reminders:
                    continue

                chat = self.db.get_chat(chat_id)
                if not chat:
                    logger.warning(f"Чат {chat_id} не найден для ежедневного уведомления")
                    continue

                # Группируем задачи по тегам для более наглядного отображения
                tasks_by_tag = {}
                for reminder in chat_reminders:
                    tag_id = reminder['tag_id']
                    if tag_id not in tasks_by_tag:
                        tasks_by_tag[tag_id] = []
                    tasks_by_tag[tag_id].append(reminder)

                # Формируем список задач на день
                tasks_text = ["Доброе утро! На сегодня запланировано:"]

                for tag_id, tag_tasks in tasks_by_tag.items():
                    tasks_text.append(f"\n🏷 {tag_id}:")
                    for task in tag_tasks:
                        due_time = parse_timestamp(task['due_time'])
                        time_str = due_time.strftime('%H:%M')
                        tasks_text.append(f"• {time_str} - {task['text']}")

                if len(tasks_text) > 1:  # Проверяем, что есть хотя бы один тег с задачами
                    message = "\n".join(tasks_text)
                    await self.bot.send_message(chat_id=chat_id, text=message)
                    logger.info(f"Отправлено ежедневное уведомление в чат {chat_id}")

        except Exception as e:
            logger.error(f"Ошибка при отправке ежедневного уведомления: {e}")

    async def monitor(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Выполняет мониторинг состояния бота.

        Args:
            context: Контекст планировщика
        """
        dt = datetime.now(SERVER_TIMEZONE)
        logger.info("Запуск мониторинга")

        try:
            # Проверяем задержки в отправке напоминаний
            reminders = self.db.get_due_reminders(dt)
            for reminder in reminders:
                reminder_time = parse_timestamp(reminder["due_time"])
                if dt - reminder_time > timedelta(minutes=5):
                    err_message = f"Напоминание {reminder['id']} не отправлено более 5 минут! Время: {reminder_time}, сейчас: {dt}"
                    logger.error(err_message)
                    await self.bot.send_message(chat_id=ADMIN_ID, text=err_message)

            # Анализируем логи на наличие ошибок
            await self._check_logs_for_errors()

        except Exception as e:
            logger.error(f"Ошибка в мониторинге: {e}")
            await self.bot.send_message(chat_id=ADMIN_ID, text=f"Ошибка в мониторинге: {e}")

    async def _check_logs_for_errors(self) -> None:
        """Проверяет логи на наличие ошибок."""
        try:
            with open("main.log", "r") as f:
                # Определяем, где остановились в прошлый раз
                if self.last_log_position > 0:
                    f.seek(self.last_log_position)

                found_errors = set()
                lines = f.readlines()

                # Запоминаем позицию для следующего запуска
                self.last_log_position = f.tell()

                for line in lines:
                    lline = line.lower()
                    if "error" in lline or "exception" in lline or "fail" in lline:
                        if len(found_errors) > 10:
                            found_errors.add("... и другие ошибки (превышен лимит вывода)")
                            break
                        found_errors.add(line[:1000])  # Обрезаем длинные строки

                if found_errors:
                    err_message = f"Обнаружены новые ошибки в логах:\n\n{'\n'.join(found_errors)}"
                    await self.bot.send_message(chat_id=ADMIN_ID, text=err_message)
        except Exception as e:
            logger.error(f"Ошибка при проверке логов: {e}")

    async def call_monitor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду запуска мониторинга.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка запуска мониторинга от неразрешенного чата: {chat_id}")
            return

        if user.id != ADMIN_ID:
            logger.warning(f"Попытка запуска мониторинга от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            await self.monitor(context)
            await update.message.reply_text("Мониторинг выполнен успешно")
            logger.info(f"Пользователь {user.id} запустил мониторинг вручную")
        except Exception as e:
            logger.error(f"Ошибка при ручном запуске мониторинга: {e}")
            await update.message.reply_text(f"Ошибка при выполнении мониторинга: {e}")

    async def call_clear_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду очистки лога.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка очистки лога от неразрешенного чата: {chat_id}")
            return

        if user.id != ADMIN_ID:
            logger.warning(f"Попытка очистки лога от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            # Сначала отправляем текущий лог
            await update.message.reply_text("Сохраняю лог перед очисткой...")
            await self.call_get_log(update, context)

            # Затем очищаем его
            with open("main.log", "w") as f:
                f.write(f"--- Лог очищен {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ---\n")

            # Сбрасываем позицию
            self.last_log_position = 0

            await update.message.reply_text("Лог успешно очищен")
            logger.info(f"Пользователь {user.id} очистил лог")
        except Exception as e:
            logger.error(f"Ошибка при очистке лога: {e}")
            await update.message.reply_text(f"Ошибка при очистке лога: {e}")

    async def call_get_log(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду получения лога.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка получения лога от неразрешенного чата: {chat_id}")
            return

        if user.id != ADMIN_ID:
            logger.warning(f"Попытка получения лога от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            await self.bot.send_document(chat_id=user.id, document=open("main.log", "rb"))
            logger.info(f"Пользователь {user.id} запросил лог")
        except Exception as e:
            logger.error(f"Ошибка при отправке лога: {e}")
            await update.message.reply_text(f"Ошибка при отправке лога: {e}")

    async def create_tag(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду создания тега.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка создания тега от неразрешенного чата: {chat_id}")
            return

        try:
            args = context.args
            if len(args) < 3:
                await update.message.reply_text(
                    "Использование: /newtag <название> <начало> <конец>\n"
                    "Пример: /newtag Работа 09:00 18:00"
                )
                return

            name = args[0]
            start_time = args[1]
            end_time = args[2]

            if not validate_time(start_time) or not validate_time(end_time):
                await update.message.reply_text("❌ Неверный формат времени. Используйте HH:MM")
                return

            if self.db.create_tag(chat_id, name, start_time, end_time):
                await update.message.reply_text(f"✅ Тег '{name}' успешно создан!")
                logger.info(f"Пользователь {user.id} создал тег '{name}' в чате {chat_id}")
            else:
                await update.message.reply_text("❌ Ошибка при создании тега")

        except Exception as e:
            logger.error(f"Ошибка при создании тега: {e}")
            await update.message.reply_text("Произошла ошибка при создании тега")

    async def allow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду предоставления доступа чату.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка предоставления доступа от неразрешенного чата: {chat_id}")
            return

        if user.id != ADMIN_ID:
            logger.warning(f"Попытка предоставления доступа от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            args = context.args
            if len(args) != 1:
                await update.message.reply_text("Использование: /allow <chat_id>")
                return

            target_chat_id = args[0]
            if not target_chat_id.lstrip('-').isdigit():
                await update.message.reply_text("Chat ID должен быть числом (может быть отрицательным для групп)")
                return

            target_chat_id = int(target_chat_id)
            target_chat = self.db.get_chat(target_chat_id)
            if not target_chat:
                self.db.create_chat({
                    'chat_id': target_chat_id,
                    'is_group': target_chat_id < 0,
                    'is_allowed': True
                })
                target_chat = self.db.get_chat(target_chat_id)

            if self.db.update_chat_permission(target_chat_id, True):
                # Обновляем кэш
                cache_key = f"chat_{target_chat_id}"
                self.chat_cache[cache_key] = True

                await update.message.reply_text(f"✅ Доступ чату '{target_chat_id}' успешно предоставлен!")
                logger.info(f"Пользователь {user.id} предоставил доступ чату {target_chat_id}")
            else:
                await update.message.reply_text("❌ Ошибка при предоставлении доступа")

        except Exception as e:
            logger.error(f"Ошибка при предоставлении доступа: {e}")
            await update.message.reply_text("Произошла ошибка при предоставлении доступа")

    async def db_tasks_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду просмотра всех задач в базе данных.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка просмотра всех задач от неразрешенного чата: {chat_id}")
            return

        if user.id != ADMIN_ID:
            logger.warning(f"Попытка просмотра всех задач от не-администратора: {user.id}")
            # Проверяем, откуда пришел запрос
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id,
                                                     text="У вас нет прав для выполнения этой команды")
            else:
                await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            all_tasks = []
            chats = self.db.list_chats()

            for chat in chats:
                tasks = self.db.list_uncompleted_reminders(chat["chat_id"])
                for task in tasks:
                    task_info = {
                        "id": task["id"],
                        "chat": f"{'Группа' if chat['is_group'] else 'Личный чат'} {chat['chat_id']}",
                        "text": task["text"],
                        "due_time": short_format_datetime(parse_timestamp(task["due_time"])),
                        "tag": task["tag_id"]
                    }
                    all_tasks.append(task_info)

            # Проверяем, не вышли ли за пределы списка
            page_size = 5
            if self.db_tasks_listing_page * page_size >= len(all_tasks):
                self.db_tasks_listing_page = 0

            # Сортируем задачи по времени
            all_tasks.sort(key=lambda x: x["due_time"])

            # Подготавливаем страницу результатов
            start_idx = self.db_tasks_listing_page * page_size
            end_idx = min(start_idx + page_size, len(all_tasks))
            page_tasks = all_tasks[start_idx:end_idx]

            if not page_tasks:
                # Отправляем сообщение в зависимости от типа запроса
                if update.callback_query:
                    await update.callback_query.message.edit_text("📋 Задачи не найдены")
                else:
                    await update.message.reply_text("📋 Задачи не найдены")
                return

            # Формируем ответ
            response_lines = [
                f"📋 Все напоминания (стр. {self.db_tasks_listing_page + 1}/{(len(all_tasks) - 1) // page_size + 1}):"]
            for task in page_tasks:
                response_lines.append(
                    f"• {task['text']} ({task['due_time']}) [{task['tag']}] - {task['chat']}"
                )

            response = "\n".join(response_lines)
            self.db_tasks_listing_page += 1

            # Добавляем кнопки навигации
            keyboard = [
                [
                    InlineKeyboardButton("⬅️ Назад", callback_data="db_tasks_prev"),
                    InlineKeyboardButton("Далее ➡️", callback_data="db_tasks_next")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Отправляем или обновляем сообщение в зависимости от типа запроса
            if update.callback_query:
                await update.callback_query.message.edit_text(response, reply_markup=reply_markup)
            else:
                await update.message.reply_text(response, reply_markup=reply_markup)

            logger.info(f"Пользователь {user.id} просмотрел страницу задач {self.db_tasks_listing_page}")

        except Exception as e:
            logger.error(f"Ошибка при просмотре задач: {e}")
            # Обрабатываем ошибку в зависимости от типа запроса
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")
            else:
                await update.message.reply_text("Произошла ошибка при получении списка задач")

    async def db_tasks_navigation(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает навигацию по списку задач.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка навигации по задачам от неразрешенного чата: {chat_id}")
            return

        if user.id != ADMIN_ID:
            logger.warning(f"Попытка навигации по задачам от не-администратора: {user.id}")
            await self.bot.answer_callback_query(update.callback_query.id, text="У вас нет прав для этой операции")
            return

        query = update.callback_query
        direction = query.data.split("_")[-1]

        try:
            if direction == "prev":
                self.db_tasks_listing_page = max(0, self.db_tasks_listing_page - 2)  # -2 т.к. в db_tasks_list будет +1

            # Запускаем обновление списка
            await self.db_tasks_list(update, context)
            await self.bot.answer_callback_query(query.id)

        except Exception as e:
            logger.error(f"Ошибка при навигации по задачам: {e}")
            await self.bot.answer_callback_query(query.id, text="Произошла ошибка")

    async def chat_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду просмотра списка чатов.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка просмотра списка чатов от неразрешенного чата: {chat_id}")
            return

        if user.id != ADMIN_ID:
            logger.warning(f"Попытка просмотра списка чатов от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            keyboard = []
            chats = self.db.list_chats()

            if not chats:
                await update.message.reply_text("📋 Чаты не найдены")
                return

            for chat_info in chats:
                status = "✅" if chat_info["is_allowed"] else "🆕"
                chat_type = "👥" if chat_info["is_group"] else "👤"
                text = f"{status} {chat_type} ({chat_info['chat_id']})"
                callback_data = f"chat_get:{chat_info['chat_id']}"
                keyboard.append([InlineKeyboardButton(text, callback_data=callback_data)])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "👥 Выберите чат для подробностей:",
                reply_markup=reply_markup
            )
            logger.info(f"Пользователь {user.id} запросил список чатов")

        except Exception as e:
            logger.error(f"Ошибка при получении списка чатов: {e}")
            await update.message.reply_text("Произошла ошибка при получении списка чатов")

    async def chat_get(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на получение информации о чате.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка получения информации о чате от неразрешенного чата: {chat_id}")
            return

        if user.id != ADMIN_ID:
            logger.warning(f"Попытка получения информации о чате от не-администратора: {user.id}")
            await self.bot.answer_callback_query(update.callback_query.id,
                                                 text="У вас нет прав для выполнения этой команды")
            return

        try:
            query = update.callback_query
            target_chat_id = int(query.data.split(":")[1])

            target_chat = self.db.get_chat(target_chat_id)
            if not target_chat:
                await self.bot.answer_callback_query(query.id, text="Чат не найден")
                return

            # Форматируем информацию о чате
            chat_info = (
                f"ℹ️ Информация о чате:\n\n"
                f"ID: {target_chat['chat_id']}\n"
                f"Тип: {'Группа' if target_chat['is_group'] else 'Личный чат'}\n"
                f"Статус: {'Активен' if target_chat['is_allowed'] else 'Не активирован'}\n"
            )

            # Добавляем статистику задач
            tasks = self.db.list_uncompleted_reminders(target_chat['chat_id'])
            chat_info += f"Активных задач: {len(tasks)}"

            # Добавляем кнопки управления чатом
            keyboard = []
            action = "Заблокировать" if target_chat['is_allowed'] else "Разблокировать"
            callback_data = f"chat_toggle:{target_chat['chat_id']}"
            keyboard.append([InlineKeyboardButton(action, callback_data=callback_data)])

            if keyboard:
                reply_markup = InlineKeyboardMarkup(keyboard)
                await self.bot.send_message(user.id, chat_info, reply_markup=reply_markup)
            else:
                await self.bot.send_message(user.id, chat_info)

            await self.bot.answer_callback_query(query.id)
            logger.info(f"Пользователь {user.id} запросил информацию о чате {target_chat_id}")

        except Exception as e:
            logger.error(f"Ошибка при получении информации о чате: {e}")
            await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def chat_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на изменение статуса чата.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка изменения статуса чата от неразрешенного чата: {chat_id}")
            return

        if user.id != ADMIN_ID:
            logger.warning(f"Попытка изменения статуса чата от не-администратора: {user.id}")
            await self.bot.answer_callback_query(update.callback_query.id, text="У вас нет прав для этого")
            return

        try:
            query = update.callback_query
            target_chat_id = int(query.data.split(":")[1])

            target_chat = self.db.get_chat(target_chat_id)
            if not target_chat:
                await self.bot.answer_callback_query(query.id, text="Чат не найден")
                return

            # Инвертируем статус
            new_status = not target_chat['is_allowed']

            if self.db.update_chat_permission(target_chat_id, new_status):
                # Обновляем кэш
                cache_key = f"chat_{target_chat_id}"
                self.chat_cache[cache_key] = new_status

                action = "разблокирован" if new_status else "заблокирован"
                await self.bot.answer_callback_query(query.id, text=f"Чат {action}")

                # Обновляем информацию о чате
                context.args = [str(target_chat_id)]
                update.callback_query.data = f"chat_get:{target_chat_id}"
                await self.chat_get(update, context)

                logger.info(f"Пользователь {user.id} изменил статус чата {target_chat_id} на {new_status}")
            else:
                await self.bot.answer_callback_query(query.id, text="Не удалось изменить статус")

        except Exception as e:
            logger.error(f"Ошибка при изменении статуса чата: {e}")
            await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def disallow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает команду отзыва доступа у чата.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка отзыва доступа от неразрешенного чата: {chat_id}")
            return

        if user.id != ADMIN_ID:
            logger.warning(f"Попытка отзыва доступа от не-администратора: {user.id}")
            await update.message.reply_text("У вас нет прав для выполнения этой команды")
            return

        try:
            args = context.args
            if len(args) != 1:
                await update.message.reply_text("Использование: /ban <chat_id>")
                return

            target_chat_id = args[0]
            if not target_chat_id.lstrip('-').isdigit():
                await update.message.reply_text("Chat ID должен быть числом (может быть отрицательным для групп)")
                return

            target_chat_id = int(target_chat_id)

            # Защита от самоблокировки
            if target_chat_id == chat_id:
                await update.message.reply_text("❌ Нельзя отозвать доступ у текущего чата")
                return

            target_chat = self.db.get_chat(target_chat_id)
            if not target_chat:
                await update.message.reply_text(f"Чат с ID {target_chat_id} не найден")
                return

            if self.db.update_chat_permission(target_chat_id, False):
                # Обновляем кэш
                cache_key = f"chat_{target_chat_id}"
                self.chat_cache[cache_key] = False

                await update.message.reply_text(f"✅ Доступ у чата {target_chat_id} успешно отозван!")
                logger.info(f"Пользователь {user.id} отозвал доступ у чата {target_chat_id}")
            else:
                await update.message.reply_text("❌ Ошибка при отзыве доступа")

        except Exception as e:
            logger.error(f"Ошибка при отзыве доступа: {e}")
            await update.message.reply_text("Произошла ошибка при отзыве доступа")

    async def list_tags(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на просмотр тегов чата.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка просмотра тегов от неразрешенного чата: {chat_id}")
            return

        try:
            tags = self.db.get_chat_tags(chat_id)

            if not tags:
                # Добавляем кнопку для быстрого создания тегов
                keyboard = [[InlineKeyboardButton("➕ Создать тег", callback_data="create_tag")]]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await self.bot.send_message(
                    chat_id,
                    "📋 У вас пока нет тегов. Создайте первый тег командой /newtag имя_тега время_начала время_окончания",
                    reply_markup=reply_markup
                )
                if update.callback_query:
                    await self.bot.answer_callback_query(update.callback_query.id)
                return

            # Создаем клавиатуру с тегами для управления
            keyboard = []
            for tag in tags:
                tag_info = f"{tag['name']} ({tag['start_time']}-{tag['end_time']})"
                callback_data = f"tag_edit:{tag['id']}"
                keyboard.append([InlineKeyboardButton(tag_info, callback_data=callback_data)])

            # Добавляем кнопку создания нового тега
            keyboard.append([InlineKeyboardButton("➕ Создать тег", callback_data="create_tag")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await self.bot.send_message(chat_id, "🏷 Ваши теги:", reply_markup=reply_markup)

            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id)

            logger.info(f"Чат {chat_id} просмотрел свои теги")

        except Exception as e:
            logger.error(f"Ошибка при просмотре тегов: {e}")
            error_message = "Произошла ошибка при получении списка тегов"

            await self.bot.send_message(chat_id, error_message)
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def list_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на просмотр напоминаний чата.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id

        if not self.is_chat_allowed(chat_id, user.id):
            logger.warning(f"Попытка просмотра напоминаний от неразрешенного чата: {chat_id}")
            return

        try:
            tasks = self.db.list_uncompleted_reminders(chat_id)

            if not tasks:
                await self.bot.send_message(chat_id, "📋 У вас пока нет напоминаний")
                if update.callback_query:
                    await self.bot.answer_callback_query(update.callback_query.id)
                return

            # Группируем задачи по дате
            grouped_tasks = {}
            today = datetime.now(SERVER_TIMEZONE).date()
            tomorrow = today + timedelta(days=1)

            for task in tasks:
                due_time = parse_timestamp(task['due_time'])

                # Определяем группу
                if due_time.date() == today:
                    date_group = "Сегодня"
                elif due_time.date() == tomorrow:
                    date_group = "Завтра"
                else:
                    date_group = format_date(due_time)

                if date_group not in grouped_tasks:
                    grouped_tasks[date_group] = []

                grouped_tasks[date_group].append(task)

            # Сортируем задачи внутри групп по времени
            for date_group in grouped_tasks:
                grouped_tasks[date_group].sort(key=lambda x: parse_timestamp(x['due_time']))

            # Формируем ответ с группировкой
            response_lines = ["📋 Ваши напоминания:"]

            for date_group in sorted(grouped_tasks.keys()):
                response_lines.append(f"\n📅 {date_group}:")

                for task in grouped_tasks[date_group]:
                    due_time = parse_timestamp(task['due_time'])
                    time_str = due_time.strftime('%H:%M')
                    response_lines.append(
                        f"• {time_str} - {task['text']} [{task['tag_id']}]"
                    )

            response = "\n".join(response_lines)

            # Добавляем кнопки фильтрации
            keyboard = [
                [InlineKeyboardButton("По тегам", callback_data="filter_tasks_by_tag")],
                [InlineKeyboardButton("По дате", callback_data="filter_tasks_by_date")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await self.bot.send_message(chat_id, response, reply_markup=reply_markup)
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id)

            logger.info(f"Чат {chat_id} просмотрел свои напоминания")

        except Exception as e:
            logger.error(f"Ошибка при просмотре напоминаний: {e}")
            error_message = "Произошла ошибка при получении списка напоминаний"

            await self.bot.send_message(chat_id, error_message)
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обрабатывает запрос на получение справки.

        Args:
            update: Объект обновления Telegram
            context: Контекст обработчика Telegram
        """
        user = update.effective_user
        chat_id = update.effective_chat.id
        is_group = chat_id < 0

        help_text = (
            "🤖 Привет! Я твой умный помощник в планировании.\n\n"
            f"{'В этой группе' if is_group else 'В личном чате'} вы можете создавать напоминания, "
            f"которые будут отправляться прямо сюда."
            f"{' Любой участник группы может добавлять и управлять напоминаниями!' if is_group else ''}\n\n"
            "Основные команды:\n"
            "• /start - Начать работу с ботом\n"
            "• /help - Показать эту справку\n"
            "• /newtag - Создать новый тег для группировки задач\n\n"
            "Примеры запросов:\n"
            "• Напомни мне позвонить маме завтра\n"
            "• Купить молоко и хлеб по пути домой вечером\n"
            "• Запланируй встречу с клиентом в четверг в 15:00\n\n"
            "Когда наступит время напоминания, я отправлю уведомление и предложу варианты переноса задачи."
        )

        try:
            await self.bot.send_message(chat_id, help_text)
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id)

            logger.info(f"Чат {chat_id} запросил справку")

        except Exception as e:
            logger.error(f"Ошибка при отправке справки: {e}")
            if update.callback_query:
                await self.bot.answer_callback_query(update.callback_query.id, text="Произошла ошибка")

    async def set_commands(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Устанавливает команды бота в меню Telegram.

        Args:
            context: Контекст планировщика
        """
        try:
            # Команды для обычных пользователей
            user_commands = [
                BotCommand("start", "Запустить бота"),
                BotCommand("help", "Показать справку"),
                BotCommand("newtag", "Добавить новый тег"),
            ]

            # Дополнительные команды для администраторов
            admin_commands = user_commands + [
                BotCommand("allow", "Предоставить доступ чату"),
                BotCommand("ban", "Отозвать доступ у чата"),
                BotCommand("list", "Список чатов"),
                BotCommand("dbtasks", "Просмотр всех напоминаний"),
                BotCommand("monitor", "Проверить состояние бота"),
                BotCommand("getlog", "Получить журнал работы"),
                BotCommand("clearlog", "Очистить журнал"),
            ]

            # Устанавливаем команды для всех пользователей
            await self.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

            # Устанавливаем расширенный список команд для администратора
            await self.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))

            logger.info("Команды бота установлены")

        except Exception as e:
            logger.error(f"Ошибка при установке команд бота: {e}")

def main() -> None:
    """Основная функция запуска бота."""
    try:
        bot = ReminderBot()
        application = ApplicationBuilder().token(BOT_TOKEN).build()

        # Обработчики команд для всех пользователей
        application.add_handler(CommandHandler("start", bot.start))
        application.add_handler(CommandHandler("newtag", bot.create_tag))
        application.add_handler(CommandHandler("help", bot.help))

        # Обработчики команд для администраторов
        application.add_handler(CommandHandler("allow", bot.allow))
        application.add_handler(CommandHandler("ban", bot.disallow))
        application.add_handler(CommandHandler("list", bot.chat_list))  # Заменено на chat_list
        application.add_handler(CommandHandler("dbtasks", bot.db_tasks_list))
        application.add_handler(CommandHandler("monitor", bot.call_monitor))
        application.add_handler(CommandHandler("getlog", bot.call_get_log))
        application.add_handler(CommandHandler("clearlog", bot.call_clear_log))

        # Обработчики сообщений и коллбэков
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message))
        application.add_handler(CallbackQueryHandler(bot.ignore, pattern="^ignore"))
        application.add_handler(CallbackQueryHandler(bot.confirm_task, pattern="^confirm_task:"))
        application.add_handler(CallbackQueryHandler(bot.reschedule_task, pattern="^reschedule_task:"))
        application.add_handler(CallbackQueryHandler(bot.complete_task, pattern="^complete_task:"))
        application.add_handler(CallbackQueryHandler(bot.list_tags, pattern="^list_tags"))
        application.add_handler(CallbackQueryHandler(bot.list_tasks, pattern="^list_tasks"))
        application.add_handler(CallbackQueryHandler(bot.chat_get, pattern="^chat_get:"))  # Заменено на chat_get
        application.add_handler(CallbackQueryHandler(bot.chat_toggle, pattern="^chat_toggle:"))  # Заменено на chat_toggle
        application.add_handler(CallbackQueryHandler(bot.db_tasks_navigation, pattern="^db_tasks_(prev|next)"))
        application.add_handler(CallbackQueryHandler(bot.help, pattern="^help"))

        # Планировщики
        application.job_queue.run_repeating(bot.check_reminders, interval=30)
        application.job_queue.run_repeating(bot.monitor, interval=1800)
        application.job_queue.run_daily(bot.daily, time=time(7, 0, tzinfo=SERVER_TIMEZONE))
        application.job_queue.run_repeating(bot.assist, interval=300)
        application.job_queue.run_once(bot.set_commands, 0)

        logger.info("Бот запущен")
        application.run_polling()

    except Exception as e:
        logger.critical(f"Критическая ошибка при запуске бота: {e}\n{traceback.format_exc()}")


if __name__ == "__main__":
    main()
