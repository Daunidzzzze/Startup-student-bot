"""
Student-side conversation handler.

States:
    REGISTER_CODE  — waiting for team code
    REGISTER_NAME  — waiting for student name
    CHATTING       — main chat state
    REFLECT_Q1     — contribution score (1-10)
    REFLECT_Q2     — cognitive load (1-5)
    REFLECT_Q3     — agent usefulness (1-5, skipped for group H)
"""
from __future__ import annotations

import html
import logging
import re
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import AI_GROUPS, GROUP_H, GROUP_AI_HEAVY, OPENAI_API_KEY
from db.database import Database
from services.ai_service import AIService
from services.multi_agent_service import MultiAgentService

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _md_to_html(text: str) -> str:
    """
    Convert basic Markdown produced by LLMs to Telegram HTML.
    Handles bold, italic, headers, inline code, horizontal rules.
    """
    # Strip [AgentName]: prefix if the model echoed it
    text = re.sub(r'^\[.+?\]:\s*', '', text.strip())

    # HTML-escape first so we don't double-escape later replacements
    text = html.escape(text)

    # Bold: **text** or __text__
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text, flags=re.DOTALL)

    # Italic: *text* (single asterisk, not at word boundary)
    text = re.sub(r'(?<!\*)\*([^\*\n]+?)\*(?!\*)', r'<i>\1</i>', text)
    # Italic: _text_ (single underscore)
    text = re.sub(r'(?<!_)_([^_\n]+?)_(?!_)', r'<i>\1</i>', text)

    # Headers: ## text  →  bold text on its own line
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)

    # Inline code: `code`
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)

    # Horizontal rule
    text = re.sub(r'^\s*---+\s*$', '', text, flags=re.MULTILINE)

    # Collapse 3+ blank lines to 2
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


# ConversationHandler states
REGISTER_CODE, REGISTER_NAME, CHATTING, REFLECT_Q1, REFLECT_Q2, REFLECT_Q3 = range(6)

_ai = AIService(OPENAI_API_KEY)
_multi_ai = MultiAgentService(OPENAI_API_KEY)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _get_student(context: ContextTypes.DEFAULT_TYPE, telegram_id: int) -> Optional[dict]:
    return await _get_db(context).get_student_by_telegram(telegram_id)


def _label_keyboard(agent_msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Использовал", callback_data=f"label:used:{agent_msg_id}"),
        InlineKeyboardButton("❌ Проигнорировал", callback_data=f"label:ignored:{agent_msg_id}"),
    ]])


# ---------------------------------------------------------------------------
# /start — registration flow
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = update.effective_user.id
    student = await _get_student(context, tg_id)
    if student:
        await update.message.reply_text(
            f"👋 С возвращением, *{student['name']}*!\n"
            f"Команда: *{student['team_name']}* ({student['group_name']})\n\n"
            "Просто напишите сообщение, чтобы начать диалог с агентом.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return CHATTING

    # Check if /start was called with a team code as argument
    args = context.args
    if args:
        code = args[0].upper()
        team = await _get_db(context).get_team_by_code(code)
        if team:
            context.user_data["reg_team"] = team
            await update.message.reply_text(
                f"🏷 Команда найдена: *{team['name']}* ({team['group_name']})\n\n"
                "Введите своё имя (Фамилия Имя):",
                parse_mode=ParseMode.MARKDOWN,
            )
            return REGISTER_NAME

    await update.message.reply_text(
        "👋 Добро пожаловать в систему эксперимента!\n\n"
        "Введите *код команды*, который выдал преподаватель:",
        parse_mode=ParseMode.MARKDOWN,
    )
    return REGISTER_CODE


async def handle_register_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    code = update.message.text.strip().upper()
    team = await _get_db(context).get_team_by_code(code)
    if not team:
        await update.message.reply_text(
            "❌ Команда с таким кодом не найдена. Проверьте код и попробуйте ещё раз:"
        )
        return REGISTER_CODE

    context.user_data["reg_team"] = team
    await update.message.reply_text(
        f"✅ Команда найдена: *{team['name']}* ({team['group_name']})\n\n"
        "Введите своё имя (Фамилия Имя):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return REGISTER_NAME


async def handle_register_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Имя слишком короткое. Введите ещё раз:")
        return REGISTER_NAME

    team = context.user_data["reg_team"]
    tg_id = update.effective_user.id
    db = _get_db(context)

    # Create student record
    student_id = await db.create_student(name, team["id"], tg_id)
    await db.log_event("session_start", f"Student registered: {name}", student_id)

    context.user_data.pop("reg_team", None)

    if team["group_name"] == GROUP_H:
        await update.message.reply_text(
            f"✅ Готово! Вы зарегистрированы как *{name}*.\n\n"
            "ℹ️ Ваша группа работает *без ИИ-агента*. "
            "Работайте над проектом самостоятельно в команде.",
            parse_mode=ParseMode.MARKDOWN,
        )
    elif team["group_name"] == GROUP_AI_HEAVY:
        agents = await db.get_team_agents(team["id"])
        agent_names = ", ".join(a["role"]["display_name"] for a in agents) if agents else "не назначены"
        await update.message.reply_text(
            f"✅ Готово! Вы зарегистрированы как *{name}*.\n\n"
            f"🤖 Состав команды агентов: *{agent_names}*\n\n"
            "Напишите сообщение — все агенты ответят и смогут реагировать друг на друга.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        role = await db.get_role_by_id(team["role_id"]) if team.get("role_id") else None
        role_name = role["display_name"] if role else "без роли"
        await update.message.reply_text(
            f"✅ Готово! Вы зарегистрированы как *{name}*.\n\n"
            f"Ваш ИИ-агент: *{role_name}*\n\n"
            "Напишите сообщение, чтобы начать диалог.",
            parse_mode=ParseMode.MARKDOWN,
        )
    return CHATTING


# ---------------------------------------------------------------------------
# Main chat handler
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = update.effective_user.id
    db = _get_db(context)
    student = await _get_student(context, tg_id)

    if not student:
        await update.message.reply_text(
            "Вы не зарегистрированы. Введите /start чтобы начать."
        )
        return CHATTING

    # Group H — no agent
    if student["group_name"] == GROUP_H:
        await update.message.reply_text(
            "ℹ️ Ваша группа работает без ИИ-агента. "
            "Обсуждайте проект со своей командой напрямую."
        )
        return CHATTING

    iteration = await db.get_active_iteration()
    if not iteration:
        await update.message.reply_text("⚠️ Нет активной итерации. Ожидайте начала занятия.")
        return CHATTING

    # Check agent availability
    agent_enabled = await db.is_agent_enabled_for_team(student["team_id"])
    if not agent_enabled:
        await update.message.reply_text(
            "🔔 На этом этапе ИИ-поддержка завершена. Продолжайте самостоятельно."
        )
        return CHATTING

    user_text = update.message.text.strip()

    # Save student message
    await db.save_message(
        student_id=student["id"],
        team_id=student["team_id"],
        iteration_id=iteration["id"],
        role="student",
        content=user_text,
    )

    # Load context history (before current message)
    history = await db.get_messages_for_context(student["id"], iteration["id"])

    # ---- Route by group ----
    if student["group_name"] == GROUP_AI_HEAVY:
        return await _handle_ai_heavy(update, context, student, iteration, history, user_text)
    else:
        return await _handle_hybrid(update, context, student, iteration, history, user_text)


# ---------------------------------------------------------------------------
# H+AI — single agent
# ---------------------------------------------------------------------------

async def _handle_hybrid(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    student: dict,
    iteration: dict,
    history: list[dict],
    user_text: str,
) -> int:
    db = _get_db(context)

    if not student.get("role_id"):
        await update.message.reply_text("⚠️ Агент не назначен для вашей команды.")
        return CHATTING

    prompt_rec = await db.get_active_prompt(student["role_id"])
    if not prompt_rec:
        await update.message.reply_text("⚠️ Системный промпт не настроен. Сообщите преподавателю.")
        return CHATTING

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        reply, latency = await _ai.get_response(
            system_prompt=prompt_rec["system_prompt"],
            history=history,
            user_message=user_text,
        )
    except Exception as exc:
        log.error("OpenAI error (hybrid): %s", exc)
        await update.message.reply_text("⚠️ Ошибка при обращении к агенту. Попробуйте ещё раз.")
        return CHATTING

    agent_msg_id = await db.save_message(
        student_id=student["id"],
        team_id=student["team_id"],
        iteration_id=iteration["id"],
        role="agent",
        agent_name=prompt_rec.get("display_name"),
        content=reply,
        prompt_version_id=prompt_rec["id"],
        latency_ms=latency,
    )

    await update.message.reply_text(
        _md_to_html(reply),
        parse_mode=ParseMode.HTML,
        reply_markup=_label_keyboard(agent_msg_id),
    )
    return CHATTING


# ---------------------------------------------------------------------------
# AI-heavy — smart routed multi-agent system
# ---------------------------------------------------------------------------

async def _handle_ai_heavy(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    student: dict,
    iteration: dict,
    history: list[dict],
    user_text: str,
) -> int:
    db = _get_db(context)

    agents = await db.get_team_agents(student["team_id"])
    if not agents:
        await update.message.reply_text(
            "⚠️ Агенты не назначены для вашей команды. Сообщите преподавателю."
        )
        return CHATTING

    # Show typing while routing + responding
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # Search notification message (shown while agent searches, deleted after)
    search_notice_msg = None

    async def on_searching(agent_name: str, query: str) -> None:
        nonlocal search_notice_msg
        try:
            search_notice_msg = await update.message.reply_text(
                f"🔍 <b>{html.escape(agent_name)}</b> ищет: <i>{html.escape(query)}</i>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    try:
        response = await _multi_ai.route_and_respond(
            agents=agents,
            history=history,
            student_message=user_text,
            on_searching=on_searching,
        )
    except Exception as exc:
        log.error("MultiAgent error: %s", exc)
        await update.message.reply_text("⚠️ Ошибка при обращении к агентам. Попробуйте ещё раз.")
        return CHATTING

    # Remove search notice before sending final answer
    if search_notice_msg:
        try:
            await search_notice_msg.delete()
        except Exception:
            pass

    # None = orchestrator decided no agent needs to respond
    if response is None:
        return CHATTING

    # Build header: agent name + search indicator
    search_tag = " 🔍" if response.search_used else ""
    header = f"🤖 <b>{html.escape(response.role_name)}</b>{search_tag}\n\n"

    agent_msg_id = await db.save_message(
        student_id=student["id"],
        team_id=student["team_id"],
        iteration_id=iteration["id"],
        role="agent",
        agent_name=response.role_name,
        content=response.content,
        prompt_version_id=response.prompt_version_id,
        latency_ms=response.latency_ms,
        round_number=response.round_number,
    )

    await update.message.reply_text(
        header + _md_to_html(response.content),
        parse_mode=ParseMode.HTML,
        reply_markup=_label_keyboard(agent_msg_id),
    )
    return CHATTING


# ---------------------------------------------------------------------------
# Label callback
# ---------------------------------------------------------------------------

async def handle_label(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")
    if len(parts) != 3:
        return
    _, label, msg_id_str = parts
    msg_id = int(msg_id_str)

    tg_id = update.effective_user.id
    db = _get_db(context)
    student = await _get_student(context, tg_id)
    if not student:
        return

    await db.save_label(msg_id, student["id"], label)

    label_text = "✅ Отмечено: использовал" if label == "used" else "❌ Отмечено: проигнорировал"
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text(label_text)


# ---------------------------------------------------------------------------
# /reflect — reflection form
# ---------------------------------------------------------------------------

async def cmd_reflect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = update.effective_user.id
    student = await _get_student(context, tg_id)
    if not student:
        await update.message.reply_text("Сначала зарегистрируйтесь: /start")
        return CHATTING

    iteration = await _get_db(context).get_active_iteration()
    if not iteration:
        await update.message.reply_text("Нет активной итерации.")
        return CHATTING

    context.user_data["reflect_iteration_id"] = iteration["id"]
    context.user_data["reflect_group"] = student["group_name"]

    await update.message.reply_text(
        f"📋 *Рефлексия — {iteration['name']}*\n\n"
        "Вопрос 1 из 3 (или 2 для группы H):\n"
        "Оцените свой вклад в работу команды на этом занятии.\n\n"
        "Введите число от *1* (минимальный) до *10* (максимальный):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return REFLECT_Q1


async def handle_reflect_q1(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = _parse_int(update.message.text, 1, 10)
    if val is None:
        await update.message.reply_text("Введите число от 1 до 10:")
        return REFLECT_Q1
    context.user_data["reflect_q1"] = val
    await update.message.reply_text(
        "Вопрос 2:\nНасколько сложным / напряжённым было это занятие?\n\n"
        "Введите число от *1* (очень легко) до *5* (очень сложно):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return REFLECT_Q2


async def handle_reflect_q2(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = _parse_int(update.message.text, 1, 5)
    if val is None:
        await update.message.reply_text("Введите число от 1 до 5:")
        return REFLECT_Q2
    context.user_data["reflect_q2"] = val

    group = context.user_data.get("reflect_group", GROUP_H)
    if group == GROUP_H:
        # Save without agent usefulness
        await _save_reflection(context, update, agent_usefulness=None)
        return CHATTING

    await update.message.reply_text(
        "Вопрос 3:\nНасколько полезным был ИИ-агент на этом занятии?\n\n"
        "Введите число от *1* (бесполезен) до *5* (очень полезен):",
        parse_mode=ParseMode.MARKDOWN,
    )
    return REFLECT_Q3


async def handle_reflect_q3(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    val = _parse_int(update.message.text, 1, 5)
    if val is None:
        await update.message.reply_text("Введите число от 1 до 5:")
        return REFLECT_Q3
    await _save_reflection(context, update, agent_usefulness=val)
    return CHATTING


async def _save_reflection(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    agent_usefulness: Optional[int],
) -> None:
    tg_id = update.effective_user.id
    db = _get_db(context)
    student = await _get_student(context, tg_id)
    if not student:
        return
    await db.save_reflection(
        student_id=student["id"],
        team_id=student["team_id"],
        iteration_id=context.user_data["reflect_iteration_id"],
        contribution=context.user_data["reflect_q1"],
        cognitive_load=context.user_data["reflect_q2"],
        agent_usefulness=agent_usefulness,
    )
    for key in ("reflect_q1", "reflect_q2", "reflect_iteration_id", "reflect_group"):
        context.user_data.pop(key, None)
    await update.message.reply_text(
        "✅ Спасибо! Ответы сохранены. Продолжайте работу над проектом."
    )


# ---------------------------------------------------------------------------
# /history — read-only view of past iterations
# ---------------------------------------------------------------------------

async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = update.effective_user.id
    db = _get_db(context)
    student = await _get_student(context, tg_id)
    if not student:
        await update.message.reply_text("Сначала зарегистрируйтесь: /start")
        return CHATTING

    iterations = await db.get_all_iterations()
    active = await db.get_active_iteration()
    past = [it for it in iterations if not it["is_active"]]

    if not past:
        await update.message.reply_text("История предыдущих занятий пока пуста.")
        return CHATTING

    keyboard = [
        [InlineKeyboardButton(it["name"], callback_data=f"history:{it['id']}")]
        for it in past
    ]
    await update.message.reply_text(
        "📜 Выберите занятие для просмотра:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CHATTING


async def handle_history_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    iteration_id = int(query.data.split(":")[1])

    tg_id = update.effective_user.id
    db = _get_db(context)
    student = await _get_student(context, tg_id)
    if not student:
        return

    messages = await db.get_messages_for_student(student["id"], iteration_id)
    if not messages:
        await query.message.reply_text("В этом занятии нет сообщений.")
        return

    lines = []
    for m in messages:
        prefix = "👤" if m["role"] == "student" else "🤖"
        ts = m["timestamp"][:16].replace("T", " ")
        lines.append(f"{prefix} [{ts}]\n{m['content']}")
        if m.get("label") == "used":
            lines.append("  _✅ использовал_")

    text = "\n\n".join(lines)
    # Split into chunks of 4000 chars
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        await query.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = update.effective_user.id
    db = _get_db(context)
    student = await _get_student(context, tg_id)
    if not student:
        await update.message.reply_text("Вы не зарегистрированы. Введите /start")
        return CHATTING

    iteration = await db.get_active_iteration()
    agent_on = await db.is_agent_enabled_for_team(student["team_id"])

    role_info = ""
    if student.get("role_id") and student["group_name"] != GROUP_H:
        role = await db.get_role_by_id(student["role_id"])
        if role:
            role_info = f"Роль агента: *{role['display_name']}*\n"

    agent_status = "🟢 активен" if agent_on else "🔴 отключён"
    iter_name = iteration["name"] if iteration else "не задана"

    await update.message.reply_text(
        f"📊 *Ваш статус*\n\n"
        f"Имя: *{student['name']}*\n"
        f"Команда: *{student['team_name']}*\n"
        f"Группа: *{student['group_name']}*\n"
        f"{role_info}"
        f"Текущее занятие: *{iter_name}*\n"
        f"ИИ-агент: {agent_status}",
        parse_mode=ParseMode.MARKDOWN,
    )
    return CHATTING


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _parse_int(text: str, lo: int, hi: int) -> Optional[int]:
    try:
        v = int(text.strip())
        return v if lo <= v <= hi else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Build ConversationHandler
# ---------------------------------------------------------------------------

def build_student_handler() -> ConversationHandler:
    return ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            REGISTER_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_register_code)
            ],
            REGISTER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_register_name)
            ],
            CHATTING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message),
                CommandHandler("status", cmd_status),
                CommandHandler("history", cmd_history),
                CommandHandler("reflect", cmd_reflect),
            ],
            REFLECT_Q1: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reflect_q1)
            ],
            REFLECT_Q2: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reflect_q2)
            ],
            REFLECT_Q3: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reflect_q3)
            ],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
        name="student_conv",
        persistent=False,
    )


def build_student_callbacks() -> list:
    """Separate callback handlers (registered outside ConversationHandler)."""
    return [
        CallbackQueryHandler(handle_label, pattern=r"^label:(used|ignored):\d+$"),
        CallbackQueryHandler(handle_history_view, pattern=r"^history:\d+$"),
    ]
