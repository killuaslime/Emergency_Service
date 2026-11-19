
import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta
import json
from pathlib import Path
import logging
from zoneinfo import ZoneInfo

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

DB_PATH = Path(__file__).resolve().parent.parent / "electricity.db"


async def init_db():
    logging.debug("Путь к базе данных: %s", DB_PATH.resolve())
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                address TEXT NOT NULL,
                supervisor_id INTEGER,
                primary_user_id INTEGER,
                panel_chat_id INTEGER,
                panel_message_id TEXT
            )
        """)

        # Миграция старой базы: добавляем поля, если их ещё нет
        cursor = await db.execute("PRAGMA table_info(shops)")
        columns = [row[1] for row in await cursor.fetchall()]

        if "panel_chat_id" not in columns:
            await db.execute(
                "ALTER TABLE shops ADD COLUMN panel_chat_id INTEGER"
            )

        if "panel_message_id" not in columns:
            await db.execute(
                "ALTER TABLE shops ADD COLUMN panel_message_id TEXT"
            )
        for col in ("lat", "lon"):
            if col not in columns:
                await db.execute(f"ALTER TABLE shops ADD COLUMN {col} REAL")
        # Опросы включения и выключения вывески
        await db.execute("""
        
            CREATE TABLE IF NOT EXISTS sign_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL,
                check_date TEXT NOT NULL,
                check_type TEXT NOT NULL,
                answered_at TEXT,
                message_chat_id INTEGER,
                message_id TEXT,
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                last_delivery_attempt_at TEXT,
                delivery_error TEXT,

                FOREIGN KEY (shop_id) REFERENCES shops(id),
                UNIQUE (shop_id, check_date, check_type)
            )
        """)

        # Состояние напоминаний по утреннему опросу
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_check_reminders (
                shop_id INTEGER NOT NULL,
                check_date TEXT NOT NULL,
                reminder_stage INTEGER NOT NULL DEFAULT 0,
                next_reminder_at TEXT,
                escalation_sent_at TEXT,
                last_message_id TEXT,
                updated_at TEXT NOT NULL,

                PRIMARY KEY (shop_id, check_date),
                FOREIGN KEY (shop_id) REFERENCES shops(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                max_user_id INTEGER UNIQUE NOT NULL,
                role TEXT NOT NULL DEFAULT 'shop',
                shop_id INTEGER,
                is_active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (shop_id) REFERENCES shops(id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS supervisors (
                max_user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                group_chat_id INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS registration_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_by INTEGER,
                decided_at TEXT,
                FOREIGN KEY (shop_id) REFERENCES shops(id)
            )
        """)

        await db.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS
            one_pending_request_per_shop
            ON registration_requests(shop_id)
            WHERE status = 'pending'
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS outages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                FOREIGN KEY (shop_id) REFERENCES shops(id)
            )
        """)
        # Миграция: поля для отложенных уведомлений
        cursor = await db.execute("PRAGMA table_info(outages)")
        columns = [row[1] for row in await cursor.fetchall()]

        if "notification_due_at" not in columns:
            await db.execute(
                "ALTER TABLE outages ADD COLUMN notification_due_at TEXT"
            )

        if "notification_sent_at" not in columns:
            await db.execute(
                "ALTER TABLE outages ADD COLUMN notification_sent_at TEXT"
            )

        if "restoration_sent_at" not in columns:
            await db.execute(
                "ALTER TABLE outages ADD COLUMN restoration_sent_at TEXT"
            )
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id INTEGER NOT NULL,
                check_date TEXT NOT NULL,
                status TEXT,
                sent_at TEXT NOT NULL,
                answered_at TEXT,
                message_chat_id INTEGER,
                message_id TEXT,

                FOREIGN KEY (shop_id) REFERENCES shops(id),
                UNIQUE (shop_id, check_date)
            )
        """)
        cursor = await db.execute("PRAGMA table_info(daily_checks)")
        columns = [row[1] for row in await cursor.fetchall()]

        if "summary_sent_at" not in columns:
            await db.execute(
                "ALTER TABLE daily_checks ADD COLUMN summary_sent_at TEXT"
            )

        # Состояние доставки опроса. Старые успешно отправленные сообщения
        # определяем по сохранённому message_id.
        if "delivery_attempts" not in columns:
            await db.execute(
                "ALTER TABLE daily_checks ADD COLUMN delivery_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "last_delivery_attempt_at" not in columns:
            await db.execute(
                "ALTER TABLE daily_checks ADD COLUMN last_delivery_attempt_at TEXT"
            )
        if "delivery_error" not in columns:
            await db.execute(
                "ALTER TABLE daily_checks ADD COLUMN delivery_error TEXT"
            )

        # Снимок текста вечернего отчёта хранится до отправки.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary_jobs (
                summary_date TEXT NOT NULL,
                summary_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                sent_at TEXT,
                last_attempt_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (summary_date, summary_type)
            )
        """)
        summary_job_columns_cursor = await db.execute(
            "PRAGMA table_info(daily_summary_jobs)"
        )
        summary_job_columns = [row[1] for row in await summary_job_columns_cursor.fetchall()]
        if "last_attempt_at" not in summary_job_columns:
            await db.execute(
                "ALTER TABLE daily_summary_jobs ADD COLUMN last_attempt_at TEXT"
            )
        if "attempts" not in summary_job_columns:
            await db.execute(
                "ALTER TABLE daily_summary_jobs ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
            )
        # Журнал отправки утренних и вечерних сводок
        await db.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary_log (
                summary_date TEXT NOT NULL,
                summary_type TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY (summary_date, summary_type)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS supervisor_daily_reports (
                supervisor_id INTEGER NOT NULL,
                check_date TEXT NOT NULL,
                message_id TEXT NOT NULL,
                PRIMARY KEY (supervisor_id, check_date)
            )
        """)
        await db.commit()


async def get_user(max_user_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                users.*,
                shops.code,
                shops.address,
                shops.supervisor_id,
                shops.panel_chat_id,
                shops.panel_message_id
            FROM users
            JOIN shops ON shops.id = users.shop_id
            WHERE users.max_user_id = ?
              AND users.is_active = 1
        """, (max_user_id,))

        return await cursor.fetchone()


async def get_shop_by_code(code: str):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT *
            FROM shops
            WHERE code = ?
        """, (code,))

        return await cursor.fetchone()


async def save_panel(shop_id: int, chat_id: int, message_id: str):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE shops
            SET panel_chat_id = ?,
                panel_message_id = ?
            WHERE id = ?
        """, (chat_id, message_id, shop_id))

        await db.commit()


async def get_shop_status(shop_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cursor = await db.execute("""
            SELECT id
            FROM outages
            WHERE shop_id = ? AND ended_at IS NULL
            LIMIT 1
        """, (shop_id,))

        return await cursor.fetchone() is not None



async def switch_power(
    shop_id: int,
    action: str,
    started_at_override: str | None = None,
    ended_at_override: str | None = None,
    send_notification: bool = True
) -> dict:
    now = datetime.now(MOSCOW_TZ)
    now_iso = now.isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")

        try:
            cursor = await db.execute("""
                SELECT id, started_at
                FROM outages
                WHERE shop_id = ? AND ended_at IS NULL
                LIMIT 1
            """, (shop_id,))

            outage = await cursor.fetchone()

            if action == "power_off":
                if outage:
                    await db.rollback()
                    return {"status": "already_off"}

                started_at = started_at_override or now_iso

                due_at = now_iso if send_notification else None

                cursor = await db.execute("""
                    INSERT INTO outages (
                        shop_id,
                        started_at,
                        notification_due_at
                    )
                    VALUES (?, ?, ?)
                """, (shop_id, started_at, due_at))

                outage_id = cursor.lastrowid

                await db.commit()

                return {
                    "status": "off",
                    "outage_id": outage_id
                }

            if action == "power_on":
                if not outage:
                    await db.rollback()
                    return {"status": "already_on"}

                started_at = datetime.fromisoformat(outage[1])
                ended_at_value = ended_at_override or now_iso
                ended_at = datetime.fromisoformat(ended_at_value)

                total_seconds = max(
                    0,
                    int((ended_at - started_at).total_seconds())
                )

                hours, remainder = divmod(total_seconds, 3600)
                minutes = remainder // 60

                duration_text = f"{hours} ч. {minutes} мин."

                if total_seconds < 60:
                    await db.execute("""
                        UPDATE outages
                        SET ended_at = ?,
                            notification_due_at = NULL
                        WHERE id = ?
                    """, (ended_at_value, outage[0]))
                else:
                    await db.execute("""
                        UPDATE outages
                        SET ended_at = ?
                        WHERE id = ?
                    """, (ended_at_value, outage[0]))

                await db.commit()

                return {
                    "status": "on",
                    "duration": duration_text,
                    "duration_seconds": total_seconds,
                    "outage_id": outage[0]
                }

            await db.rollback()
            return {"status": "invalid_action"}

        except Exception:
            await db.rollback()
            raise


async def start_morning_outage(
    shop_id: int,
    morning_time: str
) -> dict:
    """
    Закрывает предыдущее отключение в 08:15
    и создаёт новое отключение с 08:15.
    Не отправляет уведомления ни о закрытии, ни о создании.
    """

    now_iso = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")

        try:
            cursor = await db.execute("""
                SELECT id
                FROM outages
                WHERE shop_id = ?
                  AND ended_at IS NULL
                ORDER BY started_at DESC
                LIMIT 1
            """, (shop_id,))

            old_outage = await cursor.fetchone()

            old_outage_id = None

            if old_outage:
                old_outage_id = old_outage[0]

                # Закрываем старое отключение.
                # Убираем ожидающее уведомление о его начале
                # и подавляем уведомление о восстановлении.
                await db.execute("""
                    UPDATE outages
                    SET ended_at = ?,
                        notification_due_at = NULL,
                        restoration_sent_at = ?
                    WHERE id = ?
                """, (
                    morning_time,
                    now_iso,
                    old_outage_id
                ))

            # Создаём новое отключение с 08:15
            # без постановки уведомления в очередь.
            cursor = await db.execute("""
                INSERT INTO outages (
                    shop_id,
                    started_at,
                    notification_due_at
                )
                VALUES (?, ?, NULL)
            """, (shop_id, morning_time))

            new_outage_id = cursor.lastrowid

            await db.commit()

            return {
                "status": "off",
                "old_outage_id": old_outage_id,
                "new_outage_id": new_outage_id
            }

        except Exception:
            await db.rollback()
            raise

async def register_or_create_request(
    max_user_id: int,
    shop_code: str,
    address: str,
    supervisor_id: int | None
) -> str:

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("BEGIN IMMEDIATE")

        try:
            # Проверяем, не зарегистрирован ли пользователь уже
            cursor = await db.execute("""
                SELECT id
                FROM users
                WHERE max_user_id = ? AND is_active = 1
            """, (max_user_id,))

            if await cursor.fetchone():
                await db.rollback()
                return "already_registered"

            # Ищем магазин
            cursor = await db.execute("""
                SELECT id, primary_user_id
                FROM shops
                WHERE code = ?
            """, (shop_code,))

            shop = await cursor.fetchone()

            # Магазина нет в базе — создаём его
            if shop is None:
                cursor = await db.execute("""
                    INSERT INTO shops (
                        code, address, supervisor_id, primary_user_id
                    )
                    VALUES (?, ?, ?, ?)
                """, (shop_code, address, supervisor_id, max_user_id))

                shop_id = cursor.lastrowid

                await db.execute("""
                    INSERT INTO users (
                        max_user_id, role, shop_id, is_active
                    )
                    VALUES (?, 'shop', ?, 1)
                """, (max_user_id, shop_id))

                await db.commit()
                return "registered"

            shop_id, primary_user_id = shop

            # Магазин есть в справочнике, но ещё никем не зарегистрирован
            if primary_user_id is None:
                await db.execute("""
                    UPDATE shops
                    SET primary_user_id = ?,
                        supervisor_id = COALESCE(supervisor_id, ?)
                    WHERE id = ?
                """, (max_user_id, supervisor_id, shop_id))

                await db.execute("""
                    INSERT INTO users (
                        max_user_id, role, shop_id, is_active
                    )
                    VALUES (?, 'shop', ?, 1)
                """, (max_user_id, shop_id))

                await db.commit()
                return "registered"

            # Этот пользователь уже является владельцем магазина
            if primary_user_id == max_user_id:
                await db.rollback()
                return "already_registered"

            # Магазин занят другим пользователем — проверяем заявку
            cursor = await db.execute("""
                SELECT id
                FROM registration_requests
                WHERE shop_id = ? AND status = 'pending'
            """, (shop_id,))

            if await cursor.fetchone():
                await db.rollback()
                return "request_exists"

            # Создаём заявку на подтверждение
            await db.execute("""
                INSERT INTO registration_requests (
                    shop_id, requester_id
                )
                VALUES (?, ?)
            """, (shop_id, max_user_id))

            await db.commit()
            return "request_created"

        except Exception:
            await db.rollback()
            raise

async def get_latest_outage(shop_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT started_at, ended_at
            FROM outages
            WHERE shop_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (shop_id,))

        return await cursor.fetchone()

async def get_supervisor_shops(supervisor_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                s.id,
                s.code,
                s.address,

                (
                    SELECT o.started_at
                    FROM outages o
                    WHERE o.shop_id = s.id
                      AND o.ended_at IS NULL
                    ORDER BY o.started_at DESC
                    LIMIT 1
                ) AS started_at,

                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM users u
                        WHERE u.shop_id = s.id
                          AND u.is_active = 1
                    )
                    THEN 1 ELSE 0
                END AS is_registered,

                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM registration_requests r
                        WHERE r.shop_id = s.id
                          AND r.status = 'pending'
                    )
                    THEN 1 ELSE 0
                END AS has_pending_request

            FROM shops s
            WHERE s.supervisor_id = ?
            ORDER BY s.code
        """, (supervisor_id,))

        return await cursor.fetchall()


async def get_supervisor_history(
    supervisor_id: int,
    limit: int = 15
):
    """Последние отключения магазинов супервайзера."""

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                s.code,
                s.address,
                o.started_at,
                o.ended_at
            FROM outages o
            JOIN shops s ON s.id = o.shop_id
            WHERE s.supervisor_id = ?
            AND (
                o.ended_at IS NULL
                OR (
                    julianday(o.ended_at) -
                    julianday(o.started_at)
                ) * 86400 >= 60
            )
            ORDER BY o.id DESC
            LIMIT ?
        """, (supervisor_id, limit))

        return await cursor.fetchall()

async def get_daily_check_shops():
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT s.id, s.code, s.address, s.panel_chat_id, s.supervisor_id
            FROM shops s
            WHERE s.panel_chat_id IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM users u
                  WHERE u.shop_id = s.id
                    AND u.is_active = 1
              )
            ORDER BY s.code
        """)

        return await cursor.fetchall()
    
async def create_daily_check(shop_id: int, check_date: str):
    """Создаёт опрос или возвращает ID ещё не доставленного опроса.

    Возвращает None только если сообщение уже доставлено. Благодаря этому
    повторный запуск планировщика подхватывает незавершённые отправки.
    """
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cursor = await db.execute("""
            INSERT OR IGNORE INTO daily_checks (
                shop_id, check_date, sent_at
            ) VALUES (?, ?, ?)
        """, (
            shop_id,
            check_date,
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
        ))

        if cursor.rowcount:
            check_id = cursor.lastrowid
            await db.commit()
            return check_id

        cursor = await db.execute("""
            SELECT id, message_id
            FROM daily_checks
            WHERE shop_id = ? AND check_date = ?
        """, (shop_id, check_date))
        row = await cursor.fetchone()
        await db.commit()
        if row is None or row[1] is not None:
            return None
        return row[0]


async def claim_daily_check_delivery(
    check_id: int,
    retry_after_seconds: int = 60
) -> bool:
    """Атомарно захватывает попытку отправки; защищает от частых повторов."""
    now = datetime.now(MOSCOW_TZ)
    now_iso = now.isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
                UPDATE daily_checks
                SET delivery_attempts = COALESCE(delivery_attempts, 0) + 1,
                    last_delivery_attempt_at = ?,
                    delivery_error = NULL
                WHERE id = ?
                  AND message_id IS NULL
                  AND (
                    last_delivery_attempt_at IS NULL
                    OR julianday(?) - julianday(last_delivery_attempt_at)
                       >= (? / 86400.0)
                  )
            """, (now_iso, check_id, now_iso, retry_after_seconds))
            await db.commit()
            return cursor.rowcount == 1
        except Exception:
            await db.rollback()
            raise


async def mark_daily_check_delivery_failed(check_id: int, error: str):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE daily_checks
            SET delivery_error = ?
            WHERE id = ? AND message_id IS NULL
        """, (str(error)[:1000], check_id))
        await db.commit()


async def save_daily_check_message(
    check_id: int,
    chat_id: int,
    message_id: str
):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE daily_checks
            SET message_chat_id = ?,
                message_id = ?,
                delivery_error = NULL
            WHERE id = ?
        """, (chat_id, str(message_id), check_id))

        await db.commit()


async def get_daily_check(check_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT *
            FROM daily_checks
            WHERE id = ?
        """, (check_id,))

        return await cursor.fetchone()


async def answer_daily_check(
    check_id: int,
    shop_id: int,
    status: str
) -> bool:
    """Сохраняет первый ответ сотрудника на сегодняшний опрос."""

    if status not in ("on", "off"):
        return False

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")

        try:
            cursor = await db.execute("""
                UPDATE daily_checks
                SET status = ?,
                    answered_at = ?
                WHERE id = ?
                  AND shop_id = ?
                  AND status IS NULL
            """, (
                status,
                datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
                check_id,
                shop_id
            ))

            await db.commit()
            return cursor.rowcount > 0

        except Exception:
            await db.rollback()
            raise

async def delete_daily_check(check_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            "DELETE FROM daily_checks WHERE id = ?",
            (check_id,)
        )
        await db.commit()

async def sync_shops_from_json():
    shops_path = Path(__file__).resolve().parent.parent / "shops.json"

    with open(shops_path, "r", encoding="utf-8") as file:
        shops = json.load(file)

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        for shop in shops:
            await db.execute("""
                INSERT INTO shops (code, address, supervisor_id, lat, lon)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    address = excluded.address,
                    supervisor_id = excluded.supervisor_id,
                    lat = COALESCE(excluded.lat, shops.lat),
                    lon = COALESCE(excluded.lon, shops.lon)
            """, (
                shop["code"],
                shop["address"],
                shop.get("supervisor_id"),
                shop.get("lat"),
                shop.get("lon"),
            ))

        await db.commit()

    logging.info("Синхронизировано магазинов: %s", len(shops))

async def get_daily_check_results(check_date: str):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                s.code,
                s.address,
                dc.status,
                sv.group_chat_id
            FROM daily_checks dc
            JOIN shops s ON s.id = dc.shop_id
            JOIN supervisors sv ON sv.max_user_id = s.supervisor_id
            WHERE dc.check_date = ?
              AND sv.is_active = 1
            ORDER BY sv.max_user_id, s.code
        """, (check_date,))

        return await cursor.fetchall()


async def mark_daily_checks_summary_sent(check_date: str):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE daily_checks
            SET summary_sent_at = ?
            WHERE check_date = ?
              AND summary_sent_at IS NULL
        """, (
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
            check_date
        ))

        await db.commit()


async def has_pending_daily_summary(check_date: str) -> bool:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cursor = await db.execute("""
            SELECT 1
            FROM daily_checks
            WHERE check_date = ?
              AND summary_sent_at IS NULL
            LIMIT 1
        """, (check_date,))

        return await cursor.fetchone() is not None

async def get_closing_summary_shops(summary_date: str):
    """Состояние зарегистрированных магазинов на дату вечернего отчёта.

    Открытое отключение имеет приоритет. Если отключения нет, учитываем
    утренний ответ: отсутствие ответа считается unknown.
    """
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT
                s.id,
                s.code,
                s.address,
                sv.group_chat_id,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM outages o
                        WHERE o.shop_id = s.id
                          AND o.ended_at IS NULL
                    ) THEN 'off'
                    WHEN dc.status IS NULL THEN 'unknown'
                    ELSE 'on'
                END AS status
            FROM shops s
            JOIN supervisors sv
                ON sv.max_user_id = s.supervisor_id
            LEFT JOIN daily_checks dc
                ON dc.shop_id = s.id
               AND dc.check_date = ?
            WHERE sv.is_active = 1
              AND EXISTS (
                  SELECT 1
                  FROM users u
                  WHERE u.shop_id = s.id
                    AND u.is_active = 1
              )
            ORDER BY sv.max_user_id, s.code
        """, (summary_date,))
        return await cursor.fetchall()


async def has_daily_summary_been_sent(
    summary_date: str,
    summary_type: str
) -> bool:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cursor = await db.execute("""
            SELECT 1
            FROM daily_summary_log
            WHERE summary_date = ?
              AND summary_type = ?
            LIMIT 1
        """, (summary_date, summary_type))

        return await cursor.fetchone() is not None


async def mark_daily_summary_as_sent(
    summary_date: str,
    summary_type: str
):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            INSERT OR IGNORE INTO daily_summary_log (
                summary_date,
                summary_type,
                sent_at
            )
            VALUES (?, ?, ?)
        """, (
            summary_date,
            summary_type,
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
        ))

        await db.commit()


async def get_daily_summary_job(summary_date: str, summary_type: str):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT payload, sent_at
            FROM daily_summary_jobs
            WHERE summary_date = ? AND summary_type = ?
        """, (summary_date, summary_type))
        return await cursor.fetchone()


async def save_daily_summary_job(
    summary_date: str,
    summary_type: str,
    payload: str
):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            INSERT OR IGNORE INTO daily_summary_jobs (
                summary_date, summary_type, payload, created_at
            ) VALUES (?, ?, ?, ?)
        """, (
            summary_date,
            summary_type,
            payload,
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
        ))
        await db.commit()


async def claim_daily_summary_job(
    summary_date: str,
    summary_type: str,
    retry_after_seconds: int = 600
) -> bool:
    """Атомарно захватывает отправку отчёта для защиты от параллельных дублей."""
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            cursor = await db.execute("""
                UPDATE daily_summary_jobs
                SET last_attempt_at = ?,
                    attempts = COALESCE(attempts, 0) + 1
                WHERE summary_date = ?
                  AND summary_type = ?
                  AND sent_at IS NULL
                  AND (
                    last_attempt_at IS NULL
                    OR julianday(?) - julianday(last_attempt_at)
                       >= (? / 86400.0)
                  )
            """, (now, summary_date, summary_type, now, retry_after_seconds))
            await db.commit()
            return cursor.rowcount == 1
        except Exception:
            await db.rollback()
            raise


async def mark_daily_summary_job_sent(summary_date: str, summary_type: str):
    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE daily_summary_jobs
            SET sent_at = COALESCE(sent_at, ?)
            WHERE summary_date = ? AND summary_type = ?
        """, (now, summary_date, summary_type))
        await db.execute("""
            INSERT OR IGNORE INTO daily_summary_log (
                summary_date, summary_type, sent_at
            ) VALUES (?, ?, ?)
        """, (summary_date, summary_type, now))
        await db.commit()


async def get_supervisor_daily_check_results(
    supervisor_id: int,
    check_date: str
):
    """Утренние ответы только по магазинам конкретного супервайзера."""

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                s.code,
                s.address,
                dc.status,
                dc.answered_at
            FROM daily_checks dc
            JOIN shops s ON s.id = dc.shop_id
            WHERE s.supervisor_id = ?
              AND dc.check_date = ?
            ORDER BY s.code
        """, (supervisor_id, check_date))

        return await cursor.fetchall()


async def save_supervisor_daily_report(
    supervisor_id: int,
    check_date: str,
    message_id: str
):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            INSERT OR REPLACE INTO supervisor_daily_reports (
                supervisor_id,
                check_date,
                message_id
            )
            VALUES (?, ?, ?)
        """, (supervisor_id, check_date, str(message_id)))

        await db.commit()


async def get_supervisor_daily_report(
    supervisor_id: int,
    check_date: str
):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cursor = await db.execute("""
            SELECT message_id
            FROM supervisor_daily_reports
            WHERE supervisor_id = ?
              AND check_date = ?
        """, (supervisor_id, check_date))

        row = await cursor.fetchone()
        return row[0] if row else None


async def clear_supervisor_daily_report(
    supervisor_id: int,
    check_date: str
):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            DELETE FROM supervisor_daily_reports
            WHERE supervisor_id = ?
              AND check_date = ?
        """, (supervisor_id, check_date))

        await db.commit()


async def get_pending_group_notifications():
    """Получает отключения, для которых пора отправить уведомления."""

    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                o.id AS outage_id,
                o.started_at,
                o.ended_at,
                o.notification_sent_at,
                o.restoration_sent_at,
                s.code,
                s.address,
                sv.group_chat_id
            FROM outages o
            JOIN shops s ON s.id = o.shop_id
            JOIN supervisors sv ON sv.max_user_id = s.supervisor_id
            WHERE sv.is_active = 1
              AND (
                  (
                      o.notification_sent_at IS NULL
                      AND o.notification_due_at IS NOT NULL
                      AND o.notification_due_at <= ?
                  )
                  OR (
                      o.notification_sent_at IS NOT NULL
                      AND o.ended_at IS NOT NULL
                      AND o.restoration_sent_at IS NULL
                  )
              )
            ORDER BY o.id
        """, (now,))

        return await cursor.fetchall()


async def mark_outage_notification_sent(outage_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE outages
            SET notification_sent_at = ?
            WHERE id = ?
              AND notification_sent_at IS NULL
        """, (
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
            outage_id
        ))
        await db.commit()


async def mark_restoration_notification_sent(outage_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE outages
            SET restoration_sent_at = ?
            WHERE id = ?
              AND restoration_sent_at IS NULL
        """, (
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
            outage_id
        ))
        await db.commit()

async def sync_supervisors_from_json():
    path = (
        Path(__file__).resolve().parent.parent
        / "supervisors.json"
    )

    with open(path, "r", encoding="utf-8") as file:
        supervisors = json.load(file)

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        for supervisor in supervisors:
            await db.execute("""
                INSERT INTO supervisors (
                    max_user_id,
                    name,
                    group_chat_id,
                    is_active
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(max_user_id) DO UPDATE SET
                    name = excluded.name,
                    group_chat_id = excluded.group_chat_id,
                    is_active = excluded.is_active
            """, (
                supervisor["max_user_id"],
                supervisor["name"],
                supervisor["group_chat_id"],
                int(supervisor.get("active", True))
            ))

        await db.commit()

async def get_supervisor_group(supervisor_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT group_chat_id
            FROM supervisors
            WHERE max_user_id = ?
              AND is_active = 1
        """, (supervisor_id,))

        return await cursor.fetchone()

async def get_shop_group(shop_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT sv.group_chat_id
            FROM shops s
            JOIN supervisors sv
                ON sv.max_user_id = s.supervisor_id
            WHERE s.id = ?
              AND sv.is_active = 1
        """, (shop_id,))

        return await cursor.fetchone()


async def update_daily_summary_job_payload(
    summary_date: str,
    summary_type: str,
    payload: str
):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute(
            """
            UPDATE daily_summary_jobs
            SET payload = ?
            WHERE summary_date = ?
              AND summary_type = ?
            """,
            (payload, summary_date, summary_type)
        )
        await db.commit()
# ==========================================
# ОПРОСЫ ВЫВЕСКИ
# ==========================================

async def create_sign_check(shop_id: int, check_date: str, check_type: str):
    if check_type not in ("on", "off"):
        raise ValueError("Некорректный тип опроса вывески")

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            INSERT OR IGNORE INTO sign_checks (
                shop_id, check_date, check_type
            )
            VALUES (?, ?, ?)
        """, (shop_id, check_date, check_type))

        cursor = await db.execute("""
            SELECT id, message_id
            FROM sign_checks
            WHERE shop_id = ?
              AND check_date = ?
              AND check_type = ?
        """, (shop_id, check_date, check_type))

        row = await cursor.fetchone()
        await db.commit()

        if row is None or row[1] is not None:
            return None

        return row[0]


async def save_sign_check_message(
    check_id: int,
    chat_id: int,
    message_id: str
):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE sign_checks
            SET message_chat_id = ?,
                message_id = ?,
                delivery_error = NULL
            WHERE id = ?
        """, (chat_id, str(message_id), check_id))

        await db.commit()


async def answer_sign_check(
    check_id: int,
    shop_id: int
) -> bool:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cursor = await db.execute("""
            UPDATE sign_checks
            SET answered_at = ?
            WHERE id = ?
              AND shop_id = ?
              AND answered_at IS NULL
        """, (
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
            check_id,
            shop_id
        ))

        await db.commit()
        return cursor.rowcount > 0


async def get_sign_check(check_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT *
            FROM sign_checks
            WHERE id = ?
        """, (check_id,))

        return await cursor.fetchone()


# ==========================================
# НАПОМИНАНИЯ ПО УТРЕННЕМУ ОПРОСУ
# ==========================================

async def prepare_daily_check_reminders(check_date: str):
    """Создаёт состояние напоминаний для доставленных опросов."""

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            INSERT OR IGNORE INTO daily_check_reminders (
                shop_id,
                check_date,
                reminder_stage,
                next_reminder_at,
                updated_at
            )
            SELECT
                shop_id,
                check_date,
                0,
                ?,
                ?
            FROM daily_checks
            WHERE check_date = ?
              AND message_id IS NOT NULL
              AND status IS NULL
        """, (
            f"{check_date}T09:15:00+03:00",
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
            check_date
        ))

        await db.commit()

async def get_due_daily_check_reminders(check_date: str):
    """Получает магазины, которым пора отправить напоминание за указанную дату."""

    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                r.shop_id,
                r.check_date,
                r.reminder_stage,
                r.escalation_sent_at,
                s.code,
                s.address,
                s.panel_chat_id,
                s.supervisor_id,
                sv.group_chat_id,
                dc.id AS check_id
            FROM daily_check_reminders r
            JOIN daily_checks dc
                ON dc.shop_id = r.shop_id
               AND dc.check_date = r.check_date
            JOIN shops s ON s.id = r.shop_id
            LEFT JOIN supervisors sv
                ON sv.max_user_id = s.supervisor_id
               AND sv.is_active = 1
            WHERE dc.status IS NULL
              AND r.check_date = ?
              AND r.next_reminder_at IS NOT NULL
              AND r.next_reminder_at <= ?
            ORDER BY r.next_reminder_at
        """, (check_date, now))

        return await cursor.fetchall()

async def advance_daily_check_reminder(
    shop_id: int,
    check_date: str,
    expected_stage: int
):
    """Переводит напоминание на следующую стадию."""

    now = datetime.now(MOSCOW_TZ)
    now_iso = now.isoformat(timespec="seconds")

    if expected_stage == 0:
        target = datetime.fromisoformat(
            f"{check_date}T09:30:00+03:00"
        )
    elif expected_stage == 1:
        target = datetime.fromisoformat(
            f"{check_date}T10:00:00+03:00"
        )
    elif expected_stage == 2:
        target = datetime.fromisoformat(
            f"{check_date}T10:15:00+03:00"
        )
    else:
        target = now + timedelta(hours=1)

    # Если планировщик опоздал, не назначаем время в прошлом.
    if target <= now:
        target = now + timedelta(minutes=5)

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")

        try:
            cursor = await db.execute("""
                UPDATE daily_check_reminders
                SET reminder_stage = reminder_stage + 1,
                    next_reminder_at = ?,
                    updated_at = ?
                WHERE shop_id = ?
                  AND check_date = ?
                  AND reminder_stage = ?
                  AND EXISTS (
                      SELECT 1
                      FROM daily_checks
                      WHERE daily_checks.shop_id = ?
                        AND daily_checks.check_date = ?
                        AND daily_checks.status IS NULL
                  )
            """, (
                target.isoformat(timespec="seconds"),
                now_iso,
                shop_id,
                check_date,
                expected_stage,
                shop_id,
                check_date
            ))

            await db.commit()
            return cursor.rowcount == 1

        except Exception:
            await db.rollback()
            raise

async def mark_daily_check_escalation_sent(
    shop_id: int,
    check_date: str
):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE daily_check_reminders
            SET escalation_sent_at = ?,
                updated_at = ?
            WHERE shop_id = ?
              AND check_date = ?
              AND escalation_sent_at IS NULL
        """, (
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
            shop_id,
            check_date
        ))

        await db.commit()

async def stop_daily_check_reminders(shop_id: int, check_date: str):
    """Останавливает напоминания после ответа магазина."""

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE daily_check_reminders
            SET next_reminder_at = NULL,
                updated_at = ?
            WHERE shop_id = ?
              AND check_date = ?
        """, (
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds"),
            shop_id,
            check_date
        ))

        await db.commit()

async def get_sign_check_shops():
    """Получает зарегистрированные магазины для опросов вывесок."""
    return await get_daily_check_shops()


async def claim_sign_check_delivery(
    check_id: int,
    retry_after_seconds: int = 60
) -> bool:
    """Атомарно захватывает попытку отправки опроса вывески."""

    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("BEGIN IMMEDIATE")

        try:
            cursor = await db.execute("""
                UPDATE sign_checks
                SET delivery_attempts = delivery_attempts + 1,
                    last_delivery_attempt_at = ?,
                    delivery_error = NULL
                WHERE id = ?
                  AND message_id IS NULL
                  AND (
                    last_delivery_attempt_at IS NULL
                    OR julianday(?) - julianday(last_delivery_attempt_at)
                       >= (? / 86400.0)
                  )
            """, (now, check_id, now, retry_after_seconds))

            await db.commit()
            return cursor.rowcount == 1

        except Exception:
            await db.rollback()
            raise


async def mark_sign_check_delivery_failed(
    check_id: int,
    error: str
):
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("""
            UPDATE sign_checks
            SET delivery_error = ?
            WHERE id = ?
              AND message_id IS NULL
        """, (str(error)[:1000], check_id))

        await db.commit()