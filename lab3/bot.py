#!/usr/bin/env python3
"""
Telegram-бот для списка дел: приоритеты, напоминания, JSON-хранилище.
Использует python-telegram-bot (async API).
"""

from __future__ import annotations

import json
import logging
import os
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Updater, CommandHandler

# ---------------------------------------------------------------------------
# Конфигурация и константы
# ---------------------------------------------------------------------------

# Внутренние коды приоритета (в JSON)
PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"

# Слова приоритета в командах (русский + английский)
PRIORITY_ALIASES: dict[str, str] = {
    "высокий": PRIORITY_HIGH,
    "high": PRIORITY_HIGH,
    "средний": PRIORITY_MEDIUM,
    "medium": PRIORITY_MEDIUM,
    "низкий": PRIORITY_LOW,
    "low": PRIORITY_LOW,
}

# Отображение для пользователя
PRIORITY_LABEL_RU: dict[str, str] = {
    PRIORITY_HIGH: "высокий",
    PRIORITY_MEDIUM: "средний",
    PRIORITY_LOW: "низкий",
}

STATUS_LABEL_RU: dict[bool, str] = {
    False: "не выполнена",
    True: "выполнена",
}

DATETIME_FMT = "%d.%m.%Y %H:%M"
ISO_FMT = "%Y-%m-%dT%H:%M:%S"
NEWS_API_BASE = "https://newsapi.org/v2"
NEWS_PAGE_SIZE = 5

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Модель задачи и хранилище (JSON)
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """Одна задача пользователя."""

    id: int
    text: str
    created_at: str  # ISO без таймзоны (локальное время при сохранении)
    done: bool
    priority: str
    reminder_at: str | None  # ISO или None
    reminder_sent: bool  # чтобы не слать одно напоминание дважды


class TaskStore:
    """
    Потокобезопасное хранение в JSON-файле.
    Структура файла: {"version": 1, "users": { "<user_id>": {"next_id": n, "tasks": [...] } } }
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read_raw(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"version": 1, "users": {}}
        try:
            with open(self._path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.exception("Не удалось прочитать %s: %s", self._path, e)
            return {"version": 1, "users": {}}

    def _write_raw(self, data: dict[str, Any]) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(self._path)
        except OSError as e:
            logger.exception("Не удалось записать %s: %s", self._path, e)
            raise

    def _user_bucket(self, data: dict[str, Any], user_id: int) -> dict[str, Any]:
        key = str(user_id)
        users: dict[str, Any] = data.setdefault("users", {})
        if key not in users:
            users[key] = {"next_id": 1, "tasks": []}
        return users[key]

    def list_tasks(self, user_id: int) -> list[Task]:
        data = self._read_raw()
        bucket = self._user_bucket(data, user_id)
        out: list[Task] = []
        for t in bucket.get("tasks", []):
            out.append(
                Task(
                    id=int(t["id"]),
                    text=str(t["text"]),
                    created_at=str(t["created_at"]),
                    done=bool(t["done"]),
                    priority=str(t.get("priority", PRIORITY_MEDIUM)),
                    reminder_at=t.get("reminder_at"),
                    reminder_sent=bool(t.get("reminder_sent", False)),
                )
            )
        return out

    def add_task(
        self,
        user_id: int,
        text: str,
        priority: str,
        reminder_at: str | None = None,
    ) -> Task:
        data = self._read_raw()
        bucket = self._user_bucket(data, user_id)
        tid = int(bucket["next_id"])
        now = datetime.now().strftime(ISO_FMT)
        task = Task(
            id=tid,
            text=text.strip(),
            created_at=now,
            done=False,
            priority=priority,
            reminder_at=reminder_at,
            reminder_sent=False,
        )
        bucket["tasks"].append(asdict(task))
        bucket["next_id"] = tid + 1
        self._write_raw(data)
        return task

    def _find_task_index(self, tasks: list[dict[str, Any]], task_id: int) -> int:
        for i, t in enumerate(tasks):
            if int(t["id"]) == task_id:
                return i
        return -1

    def get_task(self, user_id: int, task_id: int) -> Task | None:
        data = self._read_raw()
        bucket = self._user_bucket(data, user_id)
        idx = self._find_task_index(bucket["tasks"], task_id)
        if idx < 0:
            return None
        t = bucket["tasks"][idx]
        return Task(
            id=int(t["id"]),
            text=str(t["text"]),
            created_at=str(t["created_at"]),
            done=bool(t["done"]),
            priority=str(t.get("priority", PRIORITY_MEDIUM)),
            reminder_at=t.get("reminder_at"),
            reminder_sent=bool(t.get("reminder_sent", False)),
        )

    def set_done(self, user_id: int, task_id: int, done: bool = True) -> bool:
        data = self._read_raw()
        bucket = self._user_bucket(data, user_id)
        idx = self._find_task_index(bucket["tasks"], task_id)
        if idx < 0:
            return False
        bucket["tasks"][idx]["done"] = done
        self._write_raw(data)
        return True

    def delete_task(self, user_id: int, task_id: int) -> bool:
        data = self._read_raw()
        bucket = self._user_bucket(data, user_id)
        idx = self._find_task_index(bucket["tasks"], task_id)
        if idx < 0:
            return False
        del bucket["tasks"][idx]
        self._write_raw(data)
        return True

    def set_priority(self, user_id: int, task_id: int, priority: str) -> bool:
        data = self._read_raw()
        bucket = self._user_bucket(data, user_id)
        idx = self._find_task_index(bucket["tasks"], task_id)
        if idx < 0:
            return False
        bucket["tasks"][idx]["priority"] = priority
        self._write_raw(data)
        return True

    def set_reminder(
        self,
        user_id: int,
        task_id: int,
        reminder_at: str | None,
    ) -> bool:
        data = self._read_raw()
        bucket = self._user_bucket(data, user_id)
        idx = self._find_task_index(bucket["tasks"], task_id)
        if idx < 0:
            return False
        bucket["tasks"][idx]["reminder_at"] = reminder_at
        bucket["tasks"][idx]["reminder_sent"] = False
        self._write_raw(data)
        return True

    def mark_reminder_sent(self, user_id: int, task_id: int) -> None:
        data = self._read_raw()
        bucket = self._user_bucket(data, user_id)
        idx = self._find_task_index(bucket["tasks"], task_id)
        if idx < 0:
            return
        bucket["tasks"][idx]["reminder_sent"] = True
        self._write_raw(data)

    def iter_pending_reminders(self) -> list[tuple[int, Task]]:
        """Все задачи всех пользователей с непросроченным неотправленным напоминанием."""
        data = self._read_raw()
        now = datetime.now()
        result: list[tuple[int, Task]] = []
        for uid_str, bucket in data.get("users", {}).items():
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            for t in bucket.get("tasks", []):
                if t.get("done"):
                    continue
                if t.get("reminder_sent"):
                    continue
                ra = t.get("reminder_at")
                if not ra:
                    continue
                try:
                    rt = datetime.strptime(str(ra), ISO_FMT)
                except ValueError:
                    continue
                if rt <= now:
                    task = Task(
                        id=int(t["id"]),
                        text=str(t["text"]),
                        created_at=str(t["created_at"]),
                        done=bool(t["done"]),
                        priority=str(t.get("priority", PRIORITY_MEDIUM)),
                        reminder_at=ra,
                        reminder_sent=bool(t.get("reminder_sent", False)),
                    )
                    result.append((uid, task))
        return result


# Глобальное хранилище инициализируется в main()
store: TaskStore | None = None
news_api_key: str = ""


def get_store() -> TaskStore:
    assert store is not None
    return store


def get_news_api_key() -> str:
    return news_api_key


# ---------------------------------------------------------------------------
# Вспомогательные функции парсинга и форматирования
# ---------------------------------------------------------------------------


def parse_add_command(args: list[str]) -> tuple[str, str] | None:
    """
    Разбор аргументов /add: опционально первое слово — приоритет, остальное — текст.
    Возвращает (текст, приоритет) или None при ошибке.
    """
    if not args:
        return None
    first = args[0].lower()
    if first in PRIORITY_ALIASES:
        rest = " ".join(args[1:]).strip()
        if not rest:
            return None
        return rest, PRIORITY_ALIASES[first]
    text = " ".join(args).strip()
    if not text:
        return None
    return text, PRIORITY_MEDIUM


def parse_reminder_datetime(s: str) -> datetime | None:
    """Парсинг 'ДД.ММ.ГГГГ ЧЧ:ММ'."""
    s = s.strip()
    try:
        return datetime.strptime(s, DATETIME_FMT)
    except ValueError:
        return None


def format_task_line(task: Task, index: int | None = None) -> str:
    """Одна строка задачи для списка."""
    prefix = f"{index}. " if index is not None else ""
    pr = PRIORITY_LABEL_RU.get(task.priority, task.priority)
    st = STATUS_LABEL_RU[task.done]
    created = task.created_at.replace("T", " ")[:16]
    lines = [
        f"{prefix}№{task.id} — {task.text}",
        f"   Приоритет: {pr} | Статус: {st} | Создана: {created}",
    ]
    if task.reminder_at:
        remind = str(task.reminder_at).replace("T", " ")[:16]
        sent = " (уведомление отправлено)" if task.reminder_sent else ""
        lines.append(f"   Напоминание: {remind}{sent}")
    return "\n".join(lines)


def build_tasks_message(tasks: list[Task]) -> str:
    """Текст со всеми задачами пользователя."""
    if not tasks:
        return "Список задач пуст."
    parts = ["Текущие задачи:\n"]
    for i, t in enumerate(tasks, start=1):
        parts.append(format_task_line(t, index=i))
        parts.append("")
    return "\n".join(parts).rstrip()


def _fetch_news_sync(topic: str | None = None, *, top_today: bool = False) -> tuple[bool, str]:
    """
    Синхронный запрос к NewsAPI через requests.
    Возвращает (успех, текст_ответа_для_пользователя).
    """
    api_key = get_news_api_key()
    if not api_key:
        return False, "NEWS_API_KEY не настроен. Добавьте его в .env."

    url = f"{NEWS_API_BASE}/top-headlines"
    params: dict[str, Any] = {
        "apiKey": api_key,
        "pageSize": NEWS_PAGE_SIZE,
        "language": "en",
    }
    if topic:
        params["q"] = topic
    else:
        params["country"] = "us"

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException:
        logger.exception("Ошибка запроса к NewsAPI")
        return False, "Не удалось получить новости: API недоступен или вернул ошибку."
    except ValueError:
        logger.exception("Некорректный JSON от NewsAPI")
        return False, "Не удалось обработать ответ NewsAPI."

    if data.get("status") != "ok":
        msg = data.get("message") or "неизвестная ошибка"
        logger.warning("NewsAPI status != ok: %s", msg)
        return False, f"NewsAPI вернул ошибку: {msg}"

    articles = data.get("articles") or []
    if not articles:
        return True, "По вашему запросу новостей не найдено."

    header = "Топ новостей за сегодня:" if top_today else "Последние новости:"
    lines = [header, ""]
    for i, article in enumerate(articles, start=1):
        title = (article.get("title") or "Без заголовка").strip()
        link = (article.get("url") or "").strip()
        if link:
            lines.append(f"{i}. {title}\n{link}")
        else:
            lines.append(f"{i}. {title}")
        lines.append("")
    return True, "\n".join(lines).rstrip()


async def fetch_news(topic: str | None = None, *, top_today: bool = False) -> tuple[bool, str]:
    """
    Асинхронная обертка: requests блокирующий, поэтому выносим в thread pool.
    """
    return await asyncio.to_thread(_fetch_news_sync, topic, top_today=top_today)


# ---------------------------------------------------------------------------
# Обработчики команд
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Приветствие и краткая справка."""
    try:
        text = (
            "Привет! Я бот для списка дел.\n\n"
            "Используйте /help, чтобы увидеть все команды."
        )
        if update.message:
            await update.message.reply_text(text)
    except Exception as e:
        logger.exception("cmd_start: %s", e)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать все команды (как в задании)."""
    try:
        text = (
            "Доступные команды:\n\n"
            "/add [высокий|средний|низкий] текст — добавить задачу "
            "(приоритет по умолчанию: средний)\n"
            "/list — показать все текущие задачи\n"
            "/done номер — отметить задачу выполненной\n"
            "/delete номер — удалить задачу\n"
            "/priority номер приоритет — изменить приоритет "
            "(высокий, средний, низкий)\n"
            "/reminder номер ДД.ММ.ГГГГ ЧЧ:ММ — напоминание\n"
            "/reminder номер off — убрать напоминание\n"
            "/news — показать последние новости\n"
            "/news <тема> — новости по ключевому слову\n"
            "/news_today — топ новостей за сегодня\n"
            "/help — это сообщение"
        )
        if update.message:
            await update.message.reply_text(text)
    except Exception as e:
        logger.exception("cmd_help: %s", e)


async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать последние новости или новости по теме."""
    if not update.message:
        return
    try:
        topic = " ".join(context.args).strip() if context.args else None
        ok, text = await fetch_news(topic=topic, top_today=False)
        await update.message.reply_text(text)
        if not ok:
            logger.warning("cmd_news returned warning: %s", text)
    except Exception as e:
        logger.exception("cmd_news: %s", e)
        await update.message.reply_text("Ошибка при получении новостей.")


async def cmd_news_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать топ новостей за сегодня."""
    if not update.message:
        return
    try:
        ok, text = await fetch_news(top_today=True)
        await update.message.reply_text(text)
        if not ok:
            logger.warning("cmd_news_today returned warning: %s", text)
    except Exception as e:
        logger.exception("cmd_news_today: %s", e)
        await update.message.reply_text("Ошибка при получении новостей.")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Добавить задачу: «Задача добавлена!»"""
    if not update.message:
        return
    try:
        parsed = parse_add_command(context.args or [])
        if not parsed:
            await update.message.reply_text(
                "Укажите текст задачи.\n"
                "Пример: /add Купить молоко\n"
                "Или: /add высокий Сдать лабораторную"
            )
            return
        text, priority = parsed
        get_store().add_task(update.effective_user.id, text, priority)
        await update.message.reply_text("Задача добавлена!")
    except Exception as e:
        logger.exception("cmd_add: %s", e)
        await update.message.reply_text("Не удалось добавить задачу. Попробуйте позже.")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показать задачи с номерами, приоритетом и статусом."""
    if not update.message:
        return
    try:
        tasks = get_store().list_tasks(update.effective_user.id)
        await update.message.reply_text(build_tasks_message(tasks))
    except Exception as e:
        logger.exception("cmd_list: %s", e)
        await update.message.reply_text("Не удалось загрузить список задач.")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отметить выполненной: «Задача выполнена!»"""
    if not update.message:
        return
    try:
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Использование: /done номер_задачи")
            return
        task_id = int(context.args[0])
        ok = get_store().set_done(update.effective_user.id, task_id, True)
        if ok:
            await update.message.reply_text("Задача выполнена!")
        else:
            await update.message.reply_text("Задача с таким номером не найдена.")
    except Exception as e:
        logger.exception("cmd_done: %s", e)
        await update.message.reply_text("Ошибка при обновлении задачи.")


async def cmd_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Удалить: «Задача удалена!»"""
    if not update.message:
        return
    try:
        if not context.args or not context.args[0].isdigit():
            await update.message.reply_text("Использование: /delete номер_задачи")
            return
        task_id = int(context.args[0])
        ok = get_store().delete_task(update.effective_user.id, task_id)
        if ok:
            await update.message.reply_text("Задача удалена!")
        else:
            await update.message.reply_text("Задача с таким номером не найдена.")
    except Exception as e:
        logger.exception("cmd_delete: %s", e)
        await update.message.reply_text("Ошибка при удалении задачи.")


async def cmd_priority(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Изменить приоритет задачи."""
    if not update.message:
        return
    try:
        args = context.args or []
        if len(args) < 2 or not args[0].isdigit():
            await update.message.reply_text(
                "Использование: /priority номер высокий|средний|низкий"
            )
            return
        task_id = int(args[0])
        pr_word = args[1].lower()
        if pr_word not in PRIORITY_ALIASES:
            await update.message.reply_text(
                "Приоритет: высокий, средний или низкий (или high/medium/low)."
            )
            return
        priority = PRIORITY_ALIASES[pr_word]
        ok = get_store().set_priority(update.effective_user.id, task_id, priority)
        if ok:
            label = PRIORITY_LABEL_RU[priority]
            await update.message.reply_text(f"Приоритет обновлён: {label}.")
        else:
            await update.message.reply_text("Задача с таким номером не найдена.")
    except Exception as e:
        logger.exception("cmd_priority: %s", e)
        await update.message.reply_text("Ошибка при смене приоритета.")


async def cmd_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /reminder ID ДД.ММ.ГГГГ ЧЧ:ММ
    /reminder ID off
    Дата и время — локальное время на машине, где запущен бот.
    """
    if not update.message:
        return
    try:
        args = context.args or []
        if len(args) < 2 or not args[0].isdigit():
            await update.message.reply_text(
                "Использование:\n"
                "/reminder номер ДД.ММ.ГГГГ ЧЧ:ММ\n"
                "/reminder номер off"
            )
            return
        task_id = int(args[0])
        rest = " ".join(args[1:]).strip().lower()
        if rest in ("off", "нет", "убрать", "cancel"):
            ok = get_store().set_reminder(update.effective_user.id, task_id, None)
            if ok:
                await update.message.reply_text("Напоминание снято.")
            else:
                await update.message.reply_text("Задача с таким номером не найдена.")
            return
        # Дата может содержать пробелы: "15.04.2026 09:00"
        time_part = " ".join(args[1:]).strip()
        dt = parse_reminder_datetime(time_part)
        if not dt:
            await update.message.reply_text(
                "Неверный формат даты. Нужно: ДД.ММ.ГГГГ ЧЧ:ММ (например 15.04.2026 09:00)"
            )
            return
        iso = dt.strftime(ISO_FMT)
        ok = get_store().set_reminder(update.effective_user.id, task_id, iso)
        if ok:
            await update.message.reply_text(
                f"Напоминание установлено на {dt.strftime(DATETIME_FMT)}."
            )
        else:
            await update.message.reply_text("Задача с таким номером не найдена.")
    except Exception as e:
        logger.exception("cmd_reminder: %s", e)
        await update.message.reply_text("Ошибка при установке напоминания.")


# ---------------------------------------------------------------------------
# Фоновая проверка напоминаний (истекающий срок / наступило время)
# ---------------------------------------------------------------------------


async def reminder_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Периодически проверяет задачи: если время напоминания наступило — шлём уведомление.
    Текст уведомления напоминает о задаче с «истекающим» сроком (срок = время напоминания).
    """
    st = get_store()
    try:
        pending = st.iter_pending_reminders()
    except Exception as e:
        logger.exception("reminder_tick read: %s", e)
        return

    for user_id, task in pending:
        try:
            text = (
                "Напоминание: приближается или наступило время по задаче.\n"
                f"№{task.id}: {task.text}\n"
                f"Приоритет: {PRIORITY_LABEL_RU.get(task.priority, task.priority)}"
            )
            await context.bot.send_message(chat_id=user_id, text=text)
            st.mark_reminder_sent(user_id, task.id)
        except Exception as e:
            logger.exception(
                "Не удалось отправить напоминание user=%s task=%s: %s",
                user_id,
                task.id,
                e,
            )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


def main() -> None:
    global store, news_api_key

    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env или переменных окружения.")
    news_api_key = os.environ.get("NEWS_API_KEY", "").strip()

    data_path = Path(os.environ.get("TASKS_FILE", "tasks.json")).resolve()
    store = TaskStore(data_path)
    logger.info("Файл данных: %s", data_path)

    updater = Updater(token, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", cmd_start))
    dp.add_handler(CommandHandler("help", cmd_help))
    dp.add_handler(CommandHandler("add", cmd_add))
    dp.add_handler(CommandHandler("list", cmd_list))
    dp.add_handler(CommandHandler("done", cmd_done))
    dp.add_handler(CommandHandler("delete", cmd_delete))
    dp.add_handler(CommandHandler("priority", cmd_priority))
    dp.add_handler(CommandHandler("reminder", cmd_reminder))
    dp.add_handler(CommandHandler("news", cmd_news))
    dp.add_handler(CommandHandler("news_today", cmd_news_today))

    # Проверка напоминаний каждые 60 секунд (нужен extras job-queue)
    job_queue = application.job_queue
    if job_queue is None:
        logger.warning(
            "JobQueue недоступен — напоминания не будут отправляться. "
            "Установите: pip install 'python-telegram-bot[job-queue]'"
        )
    else:
        job_queue.run_repeating(reminder_tick, interval=60, first=10)

    logger.info("Бот запущен (polling).")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
