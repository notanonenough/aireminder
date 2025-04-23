import logging
import sqlite3
from datetime import datetime
from getpass import fallback_getpass
from typing import *
from utils import *


# Класс для работы с базой данных
class Database:
    def __init__(self, db_name: str = "reminders.db"):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()
        logging.basicConfig(level=logging.INFO)

    def _create_tables(self):
        # Обновляем схему БД для работы с чатами
        self.conn.executescript('''
            -- Создаем таблицу для чатов, если она еще не существует
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                is_group BOOLEAN NOT NULL DEFAULT FALSE,
                is_allowed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            -- Обновляем таблицу тегов (теперь они связаны с чатами, а не пользователями)
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id),
                UNIQUE(chat_id, name)
            );

            -- Обновляем таблицу для неподтвержденных напоминаний
            CREATE TABLE IF NOT EXISTS pending_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                created_by INTEGER,  -- ID пользователя, создавшего напоминание
                text TEXT NOT NULL,
                tag_id TEXT,
                due_time REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
            );

            -- Обновляем таблицу для напоминаний
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                created_by INTEGER,  -- ID пользователя, создавшего напоминание
                text TEXT NOT NULL,
                tag_id TEXT,
                due_time REAL NOT NULL,
                assist TEXT,
                message_id INTEGER DEFAULT NULL,
                is_completed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES chats(chat_id)
            );

            -- Сохраняем таблицу для пользователей (для обратной совместимости)
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                is_allowed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        self.conn.commit()

    # Методы для работы с чатами
    def get_chat(self, chat_id: int) -> Optional[Dict]:
        try:
            row = self.conn.execute(
                "SELECT * FROM chats WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            logging.error(f"Error getting chat: {e}")
            return None

    def list_chats(self) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM chats"
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error listing chats: {e}")
            return []

    def create_chat(self, chat_data: Dict) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO chats (chat_id, is_group, is_allowed)
                   VALUES (?, ?, ?)""",
                (chat_data['chat_id'], chat_data.get('is_group', False),
                 chat_data.get('is_allowed', False))
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            logging.warning("Chat already exists")
            return False
        except sqlite3.Error as e:
            logging.error(f"Error creating chat: {e}")
            return False

    def update_chat_permission(self, chat_id: int, is_allowed: bool) -> bool:
        try:
            self.conn.execute(
                "UPDATE chats SET is_allowed = ? WHERE chat_id = ?",
                (is_allowed, chat_id)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error updating chat permission: {e}")
            return False

    # Теги - теперь привязаны к чатам
    def create_tag(self, chat_id: int, name: str, start_time: str, end_time: str) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO tags (chat_id, name, start_time, end_time)
                   VALUES (?, ?, ?, ?)""",
                (chat_id, name, start_time, end_time)
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError as e:
            logging.warning(f"Tag already exists for chat: {e}")
            return False
        except sqlite3.Error as e:
            logging.error(f"Error creating tag: {e}")
            return False

    def get_chat_tags(self, chat_id: int) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM tags WHERE chat_id = ?", (chat_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error getting chat tags: {e}")
            return []

    # Напоминания
    def create_unconfirmed_reminder(self, chat_id: int, text: str, due_time: datetime,
                                    created_by: int = None, tag_id: Optional[int] = None) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO pending_reminders (chat_id, created_by, text, tag_id, due_time)
                   VALUES (?, ?, ?, ?, ?)""",
                (chat_id, created_by, text, tag_id, due_time.timestamp())
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error creating unconfirmed reminder: {e}")
            return False

    def list_unconfirmed_reminders(self, chat_id: int) -> List[Dict]:
        try:
            rows = self.conn.execute(
                """SELECT * FROM pending_reminders WHERE chat_id=?""",
                (chat_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error listing unconfirmed reminders: {e}")
            return []

    def delete_unconfirmed_reminders(self, chat_id: int):
        try:
            count = self.conn.execute(
                """DELETE FROM pending_reminders WHERE chat_id=?""",
                (chat_id,)
            ).rowcount
            self.conn.commit()
            return count
        except sqlite3.Error as e:
            logging.error(f"Error deleting unconfirmed reminders: {e}")
            return 0

    def delete_unconfirmed_reminder(self, task_id: str):
        try:
            self.conn.execute(
                """DELETE FROM pending_reminders WHERE id=?""",
                (task_id,)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error deleting unconfirmed reminder: {e}")
            return False

    def get_unconfirmed_reminder(self, task_id: str) -> Dict:
        try:
            row = self.conn.execute(
                """SELECT * FROM pending_reminders WHERE id=?""",
                (task_id,)
            ).fetchone()
            return dict(row) if row else {}
        except sqlite3.Error as e:
            logging.error(f"Error getting unconfirmed reminder: {e}")
            return {}

    def get_reminder(self, task_id: str) -> Dict:
        try:
            row = self.conn.execute(
                """SELECT * FROM reminders WHERE id=?""",
                (task_id,)
            ).fetchone()
            return dict(row) if row else {}
        except sqlite3.Error as e:
            logging.error(f"Error getting reminder: {e}")
            return {}

    def reschedule(self, task_id: str, new_due_time: datetime) -> bool:
        try:
            self.conn.execute(
                """UPDATE reminders SET is_completed = FALSE, due_time=? WHERE id=?""",
                (new_due_time.timestamp(), task_id,)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error rescheduling reminder: {e}")
            return False

    def create_reminder(self, chat_id: int, text: str, due_time: datetime,
                        created_by: int = None, tag_id: Optional[int] = None) -> bool:
        try:
            self.conn.execute(
                """INSERT INTO reminders (chat_id, created_by, text, tag_id, due_time)
                   VALUES (?, ?, ?, ?, ?)""",
                (chat_id, created_by, text, tag_id, due_time.timestamp())
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error creating reminder: {e}")
            return False

    def mark_reminder_completed(self, task_id: int) -> bool:
        try:
            self.conn.execute(
                """UPDATE reminders SET is_completed = TRUE WHERE id=?""",
                (task_id,)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error marking reminder as completed: {e}")
            return False

    def list_uncompleted_reminders(self, chat_id: int) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE is_completed = FALSE AND chat_id=? ORDER BY due_time ASC", (chat_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error listing reminders: {e}")
            return []

    def list_reminders_by_tag(self, chat_id: int, tag_id: str) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE chat_id=? AND tag_id=?", (chat_id, tag_id)
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error listing reminders by tag: {e}")
            return []

    def get_due_reminders(self, dt: datetime) -> List[Dict]:
        try:
            rows = self.conn.execute(
                "SELECT * FROM reminders WHERE is_completed = FALSE AND due_time <= ?", (dt.timestamp(),)
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error(f"Error getting due reminders: {e}")
            return []

    def update_task_assist(self, task_id, assist) -> bool:
        try:
            self.conn.execute(
                "UPDATE reminders SET assist = ? WHERE id = ?",
                (assist, task_id)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error updating assist: {e}")
            return False

    def update_reminder_message_id(self, reminder_id: int, message_id: int) -> bool:
        """Сохраняет ID сообщения напоминания для последующего открепления."""
        try:
            self.conn.execute(
                """UPDATE reminders SET message_id = ? WHERE id = ?""",
                (message_id, reminder_id)
            )
            self.conn.commit()
            return True
        except sqlite3.Error as e:
            logging.error(f"Error updating reminder message_id: {e}")
            return False

    def close(self):
        self.conn.close()
