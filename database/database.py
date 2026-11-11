
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
    logging.info("Путь к базе данных: %s", DB_PATH.resolve())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA foreign_keys = ON")

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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT *
            FROM shops
            WHERE code = ?
        """, (code,))

        return await cursor.fetchone()


async def save_panel(shop_id: int, chat_id: int, message_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE shops
            SET panel_chat_id = ?,
                panel_message_id = ?
            WHERE id = ?
        """, (chat_id, message_id, shop_id))

        await db.commit()


async def get_shop_status(shop_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
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
    ended_at_override: str | None = None
) -> dict:
    now = datetime.now(MOSCOW_TZ)
    now_iso = now.isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH) as db:
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

                due_at = (
                    now + timedelta(seconds=60)
                ).isoformat(timespec="seconds")

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

                # Если отключение длилось меньше минуты,
                # отменяем ожидающее уведомление.
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

async def register_or_create_request(
    max_user_id: int,
    shop_code: str,
    address: str,
    supervisor_id: int | None
) -> str:

    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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

    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT s.id, s.code, s.address, s.panel_chat_id
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
    """Создаёт ежедневный опрос, не допуская дубликатов."""

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            INSERT OR IGNORE INTO daily_checks (
                shop_id, check_date, sent_at
            )
            VALUES (?, ?, ?)
        """, (
            shop_id,
            check_date,
            datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")
        ))

        await db.commit()

        if cursor.rowcount == 0:
            return None

        return cursor.lastrowid


async def save_daily_check_message(
    check_id: int,
    chat_id: int,
    message_id: str
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE daily_checks
            SET message_chat_id = ?,
                message_id = ?
            WHERE id = ?
        """, (chat_id, message_id, check_id))

        await db.commit()


async def get_daily_check(check_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
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

    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM daily_checks WHERE id = ?",
            (check_id,)
        )
        await db.commit()

async def sync_shops_from_json():
    shops_path = Path(__file__).resolve().parent.parent / "shops.json"

    with open(shops_path, "r", encoding="utf-8") as file:
        shops = json.load(file)

    async with aiosqlite.connect(DB_PATH) as db:
        for shop in shops:
            await db.execute("""
                INSERT INTO shops (
                    code,
                    address,
                    supervisor_id
                )
                VALUES (?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    address = excluded.address,
                    supervisor_id = excluded.supervisor_id
            """, (
                shop["code"],
                shop["address"],
                shop.get("supervisor_id")
            ))

        await db.commit()

    logging.info("Синхронизировано магазинов: %s", len(shops))

async def get_daily_check_results(check_date: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                s.code,
                s.address,
                dc.status
            FROM daily_checks dc
            JOIN shops s ON s.id = dc.shop_id
            WHERE dc.check_date = ?
            ORDER BY s.code
        """, (check_date,))

        return await cursor.fetchall()


async def mark_daily_checks_summary_sent(check_date: str):
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT 1
            FROM daily_checks
            WHERE check_date = ?
              AND summary_sent_at IS NULL
            LIMIT 1
        """, (check_date,))

        return await cursor.fetchone() is not None

async def get_closing_summary_shops():
    """
    Возвращает зарегистрированные магазины
    и их состояние электричества на момент вызова.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                s.id,
                s.code,
                s.address,
                CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM outages o
                        WHERE o.shop_id = s.id
                          AND o.ended_at IS NULL
                    )
                    THEN 'off'
                    ELSE 'on'
                END AS status
            FROM shops s
            WHERE EXISTS (
                SELECT 1
                FROM users u
                WHERE u.shop_id = s.id
                  AND u.is_active = 1
            )
            ORDER BY s.code
        """)

        return await cursor.fetchall()


async def has_daily_summary_been_sent(
    summary_date: str,
    summary_type: str
) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
async def get_supervisor_daily_check_results(
    supervisor_id: int,
    check_date: str
):
    """Утренние ответы только по магазинам конкретного супервайзера."""

    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            DELETE FROM supervisor_daily_reports
            WHERE supervisor_id = ?
              AND check_date = ?
        """, (supervisor_id, check_date))

        await db.commit()


async def get_pending_group_notifications():
    """Получает отключения, для которых пора отправить уведомления."""

    now = datetime.now(MOSCOW_TZ).isoformat(timespec="seconds")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                o.id AS outage_id,
                o.started_at,
                o.ended_at,
                o.notification_sent_at,
                o.restoration_sent_at,
                s.code,
                s.address
            FROM outages o
            JOIN shops s ON s.id = o.shop_id
            WHERE (
                o.notification_sent_at IS NULL
                AND o.notification_due_at IS NOT NULL
                AND o.notification_due_at <= ?
            )
            OR (
                o.notification_sent_at IS NOT NULL
                AND o.ended_at IS NOT NULL
                AND o.restoration_sent_at IS NULL
            )
            ORDER BY o.id
        """, (now,))

        return await cursor.fetchall()


async def mark_outage_notification_sent(outage_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
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
    async with aiosqlite.connect(DB_PATH) as db:
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
