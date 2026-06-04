"""
Entry point — assembles the Telegram Application and starts polling.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import Application

import config
from db.database import Database
from handlers import build_admin_handlers, build_student_handler
from handlers.student import build_student_callbacks
from services.export_service import ExportService

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)


async def post_init(application: Application) -> None:
    """Initialise DB, services, and register bot commands."""
    db = Database(config.DATABASE_PATH)
    await db.connect()
    application.bot_data["db"] = db
    application.bot_data["export"] = ExportService(db)
    log.info("Database connected: %s", config.DATABASE_PATH)

    await application.bot.set_my_commands([
        BotCommand("start", "Войти в систему / начать работу"),
        BotCommand("status", "Текущее занятие и статус агента"),
        BotCommand("reflect", "Заполнить рефлексию по итерации"),
        BotCommand("history", "Просмотреть историю прошлых занятий"),
        BotCommand("help", "Справка"),
    ])


async def post_shutdown(application: Application) -> None:
    db: Database = application.bot_data.get("db")
    if db:
        await db.close()
        log.info("Database connection closed.")


async def _inactivity_monitor(application: Application) -> None:
    """Background task: alerts admins about inactive AI-group students."""
    while True:
        await asyncio.sleep(config.INACTIVITY_ALERT_MINUTES * 60)
        try:
            db: Database = application.bot_data["db"]
            inactive = await db.get_inactive_students(config.INACTIVITY_ALERT_MINUTES)
            if inactive:
                names = ", ".join(f"{s['name']} ({s['team_name']})" for s in inactive)
                msg = (
                    f"⚠️ Неактивны более {config.INACTIVITY_ALERT_MINUTES} мин.:\n{names}"
                )
                for admin_id in config.ADMIN_TELEGRAM_IDS:
                    try:
                        await application.bot.send_message(chat_id=admin_id, text=msg)
                    except Exception:
                        pass
        except Exception as exc:
            log.warning("Inactivity monitor error: %s", exc)


def main() -> None:
    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Register student ConversationHandler (highest priority)
    app.add_handler(build_student_handler(), group=0)

    # Register label + history callbacks (before admin text handler)
    for handler in build_student_callbacks():
        app.add_handler(handler, group=1)

    # Register admin handlers
    for handler in build_admin_handlers():
        app.add_handler(handler, group=2)

    # /help command
    async def cmd_help(update, context):
        await update.message.reply_text(
            "🤖 *Команды бота:*\n\n"
            "/start — войти / зарегистрироваться\n"
            "/status — ваш статус и текущее занятие\n"
            "/reflect — заполнить рефлексию\n"
            "/history — история прошлых занятий\n\n"
            "Просто напишите сообщение — оно уйдёт вашему ИИ-агенту.\n\n"
            "По вопросам обращайтесь к преподавателю.",
            parse_mode="Markdown",
        )

    from telegram.ext import CommandHandler as CH
    app.add_handler(CH("help", cmd_help), group=3)

    # Start background monitor
    if config.ADMIN_TELEGRAM_IDS:
        app.job_queue.run_repeating(
            lambda ctx: asyncio.ensure_future(_inactivity_monitor_once(ctx.application)),
            interval=config.INACTIVITY_ALERT_MINUTES * 60,
            first=config.INACTIVITY_ALERT_MINUTES * 60,
        )

    log.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)


async def _inactivity_monitor_once(application: Application) -> None:
    try:
        db: Database = application.bot_data["db"]
        inactive = await db.get_inactive_students(config.INACTIVITY_ALERT_MINUTES)
        if inactive:
            names = "\n".join(f"• {s['name']} ({s['team_name']})" for s in inactive)
            msg = f"⚠️ Не активны более {config.INACTIVITY_ALERT_MINUTES} мин.:\n{names}"
            for admin_id in config.ADMIN_TELEGRAM_IDS:
                try:
                    await application.bot.send_message(chat_id=admin_id, text=msg)
                except Exception:
                    pass
    except Exception as exc:
        log.warning("Inactivity check error: %s", exc)


if __name__ == "__main__":
    main()
