"""
Admin panel handlers.

Navigation is entirely inline-keyboard-based.
Text input is handled by checking context.user_data['admin_state'].

States stored in user_data:
    admin_state       — current input mode (string or None)
    admin_temp        — dict for accumulating multi-step form data
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import ADMIN_TELEGRAM_IDS, AI_GROUPS, GROUP_H
from db.database import Database
from services.export_service import ExportService

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Admin state keys
# ---------------------------------------------------------------------------

ST_NONE = None
ST_ADD_TEAM_NAME = "add_team_name"
ST_ADD_TEAM_CODE = "add_team_code"
ST_ADD_TEAM_GROUP = "add_team_group"
ST_ADD_TEAM_ROLE = "add_team_role"
ST_ADD_STUDENT_NAME = "add_student_name"
ST_ADD_STUDENT_TEAM = "add_student_team"
ST_EDIT_PROMPT_TEXT = "edit_prompt_text"
ST_BROADCAST = "broadcast_msg"
ST_ADD_ROLE_NAME = "add_role_name"
ST_ADD_ROLE_DISPLAY = "add_role_display"
ST_ADD_ROLE_PROMPT = "add_role_prompt"


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def is_admin(update: Update) -> bool:
    return update.effective_user.id in ADMIN_TELEGRAM_IDS


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _exp(context: ContextTypes.DEFAULT_TYPE) -> ExportService:
    return context.application.bot_data["export"]


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Участники и команды", callback_data="adm:participants")],
        [InlineKeyboardButton("🏃 Ход эксперимента", callback_data="adm:experiment")],
        [InlineKeyboardButton("✏️ Промпты агентов", callback_data="adm:prompts")],
        [InlineKeyboardButton("📊 Аналитика", callback_data="adm:analytics")],
        [InlineKeyboardButton("📤 Экспорт данных", callback_data="adm:export")],
        [InlineKeyboardButton("🔴 Мониторинг", callback_data="adm:monitor")],
    ])


def kb_back(target: str = "adm:main") -> list:
    return [[InlineKeyboardButton("⬅️ Назад", callback_data=target)]]


# ---------------------------------------------------------------------------
# /admin entry
# ---------------------------------------------------------------------------

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("❌ Доступ запрещён.")
        return
    context.user_data["admin_state"] = ST_NONE
    context.user_data["admin_temp"] = {}
    await update.message.reply_text(
        "🎓 *Панель исследователя*\n\nВыберите раздел:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main(),
    )


# ---------------------------------------------------------------------------
# Text input router (catches admin text when in an input state)
# ---------------------------------------------------------------------------

async def handle_admin_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return
    state = context.user_data.get("admin_state", ST_NONE)
    if state is None:
        return
    text = update.message.text.strip()
    router = {
        ST_ADD_TEAM_NAME: _at_team_name,
        ST_ADD_TEAM_CODE: _at_team_code,
        ST_ADD_STUDENT_NAME: _at_student_name,
        ST_EDIT_PROMPT_TEXT: _at_prompt_text,
        ST_BROADCAST: _at_broadcast,
        ST_ADD_ROLE_NAME: _at_role_name,
        ST_ADD_ROLE_DISPLAY: _at_role_display,
        ST_ADD_ROLE_PROMPT: _at_role_prompt,
    }
    handler = router.get(state)
    if handler:
        await handler(update, context, text)


# ---------------------------------------------------------------------------
# Callback router
# ---------------------------------------------------------------------------

async def handle_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.callback_query.answer("Доступ запрещён.")
        return
    query = update.callback_query
    await query.answer()
    data = query.data

    routes = {
        "adm:main": _show_main,
        "adm:participants": _show_participants,
        "adm:experiment": _show_experiment,
        "adm:prompts": _show_prompts,
        "adm:analytics": _show_analytics,
        "adm:export": _show_export,
        "adm:monitor": _show_monitor,
        "adm:add_team": _start_add_team,
        "adm:list_teams": _list_teams,
        "adm:list_students": _list_students,
        "adm:add_student": _start_add_student,
        "adm:add_role": _start_add_role,
    }

    if data in routes:
        await routes[data](update, context)
    elif data.startswith("adm:iter:"):
        await _switch_iteration(update, context, int(data.split(":")[2]))
    elif data.startswith("adm:agent_off:"):
        team_id = data.split(":")[2]
        await _disable_agent(update, context, int(team_id) if team_id != "all" else None)
    elif data.startswith("adm:agent_on:"):
        team_id = data.split(":")[2]
        await _enable_agent(update, context, int(team_id) if team_id != "all" else None)
    elif data.startswith("adm:edit_prompt:"):
        await _edit_prompt(update, context, int(data.split(":")[2]))
    elif data.startswith("adm:export:"):
        await _do_export(update, context, data.split(":", 2)[2])
    elif data.startswith("adm:broadcast"):
        await _start_broadcast(update, context)
    elif data.startswith("adm:team_role:"):
        parts = data.split(":")
        await _assign_team_role(update, context, int(parts[2]), int(parts[3]))
    elif data.startswith("adm:del_team:"):
        await _delete_team(update, context, int(data.split(":")[2]))
    elif data.startswith("adm:del_student:"):
        await _delete_student(update, context, int(data.split(":")[2]))
    elif data.startswith("adm:add_team_group:"):
        group = data.split(":", 2)[2]
        await _at_team_group(update, context, group)
    elif data.startswith("adm:add_team_role:"):
        parts = data.split(":")
        await _at_team_role(update, context, int(parts[2]) if parts[2] != "none" else None)
    elif data.startswith("adm:add_student_team:"):
        await _at_student_team(update, context, int(data.split(":")[2]))
    elif data.startswith("adm:manage_agents:"):
        await _manage_team_agents(update, context, int(data.split(":")[2]))
    elif data.startswith("adm:add_agent:"):
        parts = data.split(":")
        await _add_team_agent(update, context, int(parts[2]), int(parts[3]))
    elif data.startswith("adm:rm_agent:"):
        parts = data.split(":")
        await _remove_team_agent(update, context, int(parts[2]), int(parts[3]))


# ---------------------------------------------------------------------------
# Section: main
# ---------------------------------------------------------------------------

async def _show_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["admin_state"] = ST_NONE
    context.user_data["admin_temp"] = {}
    await update.callback_query.edit_message_text(
        "🎓 *Панель исследователя*\n\nВыберите раздел:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_main(),
    )


# ---------------------------------------------------------------------------
# Section: participants
# ---------------------------------------------------------------------------

async def _show_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Добавить команду", callback_data="adm:add_team")],
        [InlineKeyboardButton("👤 Добавить студента", callback_data="adm:add_student")],
        [InlineKeyboardButton("📋 Список команд", callback_data="adm:list_teams")],
        [InlineKeyboardButton("📋 Список студентов", callback_data="adm:list_students")],
        [InlineKeyboardButton("➕ Добавить роль агента", callback_data="adm:add_role")],
        *kb_back(),
    ])
    await update.callback_query.edit_message_text(
        "👥 *Участники и команды*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb
    )


async def _list_teams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    teams = await db.get_all_teams()
    if not teams:
        text = "Команд пока нет."
        await update.callback_query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb_back("adm:participants")),
        )
        return

    lines = []
    for t in teams:
        if t["group_name"] == "AI-heavy":
            agents = await db.get_team_agents(t["id"])
            agent_str = ", ".join(a["role"]["display_name"] for a in agents) if agents else "нет агентов"
            lines.append(f"• *{t['name']}* `{t['code']}` | AI-heavy | 🤖 {agent_str}")
        else:
            role = t.get("role_display") or "—"
            lines.append(f"• *{t['name']}* `{t['code']}` | {t['group_name']} | 🤖 {role}")

    # Add manage-agents buttons for AI-heavy teams
    kb_rows = []
    for t in teams:
        if t["group_name"] == "AI-heavy":
            kb_rows.append([InlineKeyboardButton(
                f"⚙️ Агенты: {t['name']}",
                callback_data=f"adm:manage_agents:{t['id']}"
            )])
    kb_rows.extend(kb_back("adm:participants"))

    await update.callback_query.edit_message_text(
        "📋 *Команды:*\n\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def _manage_team_agents(update: Update, context: ContextTypes.DEFAULT_TYPE, team_id: int) -> None:
    """Show agents assigned to an AI-heavy team with add/remove buttons."""
    db = _db(context)
    team = await db.get_team_by_id(team_id)
    all_roles = await db.get_roles()
    assigned = await db.get_team_agents(team_id)
    assigned_ids = {a["role"]["id"] for a in assigned}

    lines = ["*Назначенные агенты:*"]
    kb_rows = []
    for a in assigned:
        lines.append(f"  🤖 {a['role']['display_name']}")
        kb_rows.append([InlineKeyboardButton(
            f"❌ Убрать {a['role']['display_name']}",
            callback_data=f"adm:rm_agent:{team_id}:{a['role']['id']}"
        )])

    lines.append("\n*Добавить агента:*")
    for r in all_roles:
        if r["id"] not in assigned_ids:
            kb_rows.append([InlineKeyboardButton(
                f"➕ {r['display_name']}",
                callback_data=f"adm:add_agent:{team_id}:{r['id']}"
            )])

    kb_rows.extend([[InlineKeyboardButton("⬅️ Назад к командам", callback_data="adm:list_teams")]])

    await update.callback_query.edit_message_text(
        f"⚙️ *Агенты команды «{team['name']}»*\n\n" + "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def _list_students(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    students = await _db(context).get_all_students()
    if not students:
        text = "Студентов пока нет."
    else:
        lines = [
            f"• *{s['name']}* | {s['team_name']} ({s['group_name']}) "
            f"{'✅' if s['telegram_id'] else '⬜'}"
            for s in students
        ]
        text = "📋 *Студенты* (✅ = зарегистрирован в боте):\n\n" + "\n".join(lines)

    await update.callback_query.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_back("adm:participants")),
    )


# ---------------------------------------------------------------------------
# Add team flow
# ---------------------------------------------------------------------------

async def _start_add_team(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["admin_state"] = ST_ADD_TEAM_NAME
    context.user_data["admin_temp"] = {}
    await update.callback_query.edit_message_text(
        "➕ *Новая команда*\n\nВведите название команды:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_back("adm:participants")),
    )


async def _at_team_name(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    context.user_data["admin_temp"]["name"] = text
    context.user_data["admin_state"] = ST_ADD_TEAM_CODE
    await update.message.reply_text(
        f"Название: *{text}*\n\nТеперь введите короткий *код команды* (например, TEAM01A).\n"
        "Студенты будут использовать его для входа:",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _at_team_code(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    code = text.upper()
    existing = await _db(context).get_team_by_code(code)
    if existing:
        await update.message.reply_text(f"❌ Код `{code}` уже занят. Введите другой:", parse_mode=ParseMode.MARKDOWN)
        return
    context.user_data["admin_temp"]["code"] = code
    context.user_data["admin_state"] = ST_ADD_TEAM_GROUP
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("H (без агента)", callback_data="adm:add_team_group:H")],
        [InlineKeyboardButton("H+AI (гибридная)", callback_data="adm:add_team_group:H+AI")],
        [InlineKeyboardButton("AI-heavy", callback_data="adm:add_team_group:AI-heavy")],
    ])
    await update.message.reply_text("Выберите группу:", reply_markup=kb)


async def _at_team_group(update: Update, context: ContextTypes.DEFAULT_TYPE, group: str) -> None:
    context.user_data["admin_temp"]["group"] = group
    if group == GROUP_H:
        # No agent needed
        await _finish_add_team(update, context, role_id=None)
        return
    context.user_data["admin_state"] = ST_ADD_TEAM_ROLE
    roles = await _db(context).get_roles()
    kb_rows = [[InlineKeyboardButton(r["display_name"], callback_data=f"adm:add_team_role:{r['id']}")] for r in roles]
    kb_rows.append([InlineKeyboardButton("Без роли", callback_data="adm:add_team_role:none")])
    await update.callback_query.edit_message_text(
        "Выберите роль агента для команды:",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def _at_team_role(update: Update, context: ContextTypes.DEFAULT_TYPE, role_id: Optional[int]) -> None:
    await _finish_add_team(update, context, role_id)


async def _finish_add_team(update: Update, context: ContextTypes.DEFAULT_TYPE, role_id: Optional[int]) -> None:
    temp = context.user_data["admin_temp"]
    team_id = await _db(context).create_team(
        name=temp["name"], code=temp["code"], group_name=temp["group"], role_id=role_id
    )
    context.user_data["admin_state"] = ST_NONE
    context.user_data["admin_temp"] = {}
    msg = (
        f"✅ Команда *{temp['name']}* создана!\n"
        f"Код: `{temp['code']}`\n"
        f"Группа: {temp['group']}\n\n"
        f"Ссылка для студентов:\n`/start {temp['code']}`"
    )
    target = update.callback_query if update.callback_query else update.message
    if update.callback_query:
        await update.callback_query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN,
                                                       reply_markup=InlineKeyboardMarkup(kb_back("adm:participants")))
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Add student flow
# ---------------------------------------------------------------------------

async def _start_add_student(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["admin_state"] = ST_ADD_STUDENT_NAME
    context.user_data["admin_temp"] = {}
    await update.callback_query.edit_message_text(
        "👤 *Добавить студента*\n\nВведите имя студента:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_back("adm:participants")),
    )


async def _at_student_name(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    context.user_data["admin_temp"]["name"] = text
    context.user_data["admin_state"] = ST_ADD_STUDENT_TEAM
    teams = await _db(context).get_all_teams()
    if not teams:
        await update.message.reply_text("❌ Сначала создайте хотя бы одну команду.")
        context.user_data["admin_state"] = ST_NONE
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{t['name']} ({t['group_name']})", callback_data=f"adm:add_student_team:{t['id']}")]
        for t in teams
    ])
    await update.message.reply_text("Выберите команду:", reply_markup=kb)


async def _at_student_team(update: Update, context: ContextTypes.DEFAULT_TYPE, team_id: int) -> None:
    name = context.user_data["admin_temp"]["name"]
    student_id = await _db(context).create_student(name, team_id)
    context.user_data["admin_state"] = ST_NONE
    context.user_data["admin_temp"] = {}
    team = await _db(context).get_team_by_id(team_id)
    await update.callback_query.edit_message_text(
        f"✅ Студент *{name}* добавлен в команду *{team['name']}*.\n\n"
        f"Пусть студент отправит боту: `/start {team['code']}`",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_back("adm:participants")),
    )


async def _delete_team(update: Update, context: ContextTypes.DEFAULT_TYPE, team_id: int) -> None:
    await _db(context).delete_team(team_id)
    await update.callback_query.answer("Команда удалена.")
    await _list_teams(update, context)


async def _delete_student(update: Update, context: ContextTypes.DEFAULT_TYPE, student_id: int) -> None:
    await _db(context).delete_student(student_id)
    await update.callback_query.answer("Студент удалён.")
    await _list_students(update, context)


# ---------------------------------------------------------------------------
# Add custom role flow
# ---------------------------------------------------------------------------

async def _start_add_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["admin_state"] = ST_ADD_ROLE_NAME
    context.user_data["admin_temp"] = {}
    await update.callback_query.edit_message_text(
        "➕ *Новая роль агента*\n\nВведите внутреннее имя роли (латиницей, без пробелов, например: `mentor`):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_back("adm:participants")),
    )


async def _at_role_name(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    name = text.lower().replace(" ", "_")
    context.user_data["admin_temp"]["name"] = name
    context.user_data["admin_state"] = ST_ADD_ROLE_DISPLAY
    await update.message.reply_text(f"Внутреннее имя: `{name}`\n\nВведите отображаемое название (по-русски):", parse_mode=ParseMode.MARKDOWN)


async def _at_role_display(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    context.user_data["admin_temp"]["display"] = text
    context.user_data["admin_state"] = ST_ADD_ROLE_PROMPT
    await update.message.reply_text(
        f"Отображение: *{text}*\n\nВведите системный промпт для этой роли:",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _at_role_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    temp = context.user_data["admin_temp"]
    tg_user = str(update.effective_user.id)
    role_id = await _db(context).add_role(temp["name"], temp["display"], text, tg_user)
    context.user_data["admin_state"] = ST_NONE
    context.user_data["admin_temp"] = {}
    await update.message.reply_text(
        f"✅ Роль *{temp['display']}* (`{temp['name']}`) создана (ID={role_id}).",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Section: experiment control
# ---------------------------------------------------------------------------

async def _show_experiment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    iteration = await db.get_active_iteration()
    all_iters = await db.get_all_iterations()

    iter_name = iteration["name"] if iteration else "не задана"
    agent_status = "🟢 активны" if (iteration and iteration["agent_enabled"]) else "🔴 отключены"

    iter_buttons = [
        [InlineKeyboardButton(
            f"{'▶️ ' if it['is_active'] else ''}{it['name']}",
            callback_data=f"adm:iter:{it['number']}"
        )]
        for it in all_iters
    ]

    kb = InlineKeyboardMarkup([
        *iter_buttons,
        [InlineKeyboardButton("🔴 Отключить агентов (все)", callback_data="adm:agent_off:all")],
        [InlineKeyboardButton("🟢 Включить агентов (все)", callback_data="adm:agent_on:all")],
        [InlineKeyboardButton("📢 Запросить рефлексию", callback_data="adm:broadcast_reflect")],
        [InlineKeyboardButton("📣 Широковещательное сообщение", callback_data="adm:broadcast")],
        *kb_back(),
    ])
    await update.callback_query.edit_message_text(
        f"🏃 *Ход эксперимента*\n\n"
        f"Текущая итерация: *{iter_name}*\n"
        f"ИИ-агенты: {agent_status}\n\n"
        "Выберите итерацию для активации или управляйте агентами:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def _switch_iteration(update: Update, context: ContextTypes.DEFAULT_TYPE, number: int) -> None:
    db = _db(context)
    await db.set_active_iteration(number)
    # Notify all registered students
    iters = await db.get_all_iterations()
    it = next((i for i in iters if i["number"] == number), None)
    name = it["name"] if it else f"Занятие {number}"
    students = await db.get_students_with_telegram()
    for s in students:
        try:
            await context.bot.send_message(
                chat_id=s["telegram_id"],
                text=f"🔔 Преподаватель переключил итерацию.\n\n*Теперь активно: {name}*",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass
    await update.callback_query.answer(f"Активирована: {name}")
    await _show_experiment(update, context)


async def _disable_agent(update: Update, context: ContextTypes.DEFAULT_TYPE, team_id: Optional[int]) -> None:
    db = _db(context)
    await db.set_agent_enabled(False, team_id)
    students = await db.get_students_with_telegram()
    for s in students:
        if team_id and s.get("team_id") != team_id:
            continue
        if s["group_name"] == GROUP_H:
            continue
        try:
            await context.bot.send_message(
                chat_id=s["telegram_id"],
                text="🔔 На этом этапе ИИ-поддержка завершена. Продолжайте самостоятельно.",
            )
        except Exception:
            pass
    await update.callback_query.answer("Агенты отключены.")
    await _show_experiment(update, context)


async def _enable_agent(update: Update, context: ContextTypes.DEFAULT_TYPE, team_id: Optional[int]) -> None:
    await _db(context).set_agent_enabled(True, team_id)
    await update.callback_query.answer("Агенты включены.")
    await _show_experiment(update, context)


# ---------------------------------------------------------------------------
# Section: broadcast
# ---------------------------------------------------------------------------

async def _start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = update.callback_query.data
    if data == "adm:broadcast_reflect":
        # Send reflection request
        db = _db(context)
        iteration = await db.get_active_iteration()
        if not iteration:
            await update.callback_query.answer("Нет активной итерации.")
            return
        students = await db.get_students_with_telegram()
        sent = 0
        for s in students:
            try:
                await context.bot.send_message(
                    chat_id=s["telegram_id"],
                    text=f"📋 Пожалуйста, заполните рефлексию по *{iteration['name']}*.\n\nНажмите /reflect",
                    parse_mode=ParseMode.MARKDOWN,
                )
                sent += 1
            except Exception:
                pass
        await update.callback_query.answer(f"Отправлено {sent} студентам.")
        return

    context.user_data["admin_state"] = ST_BROADCAST
    await update.callback_query.edit_message_text(
        "📣 Введите сообщение для рассылки всем студентам:",
        reply_markup=InlineKeyboardMarkup(kb_back("adm:experiment")),
    )


async def _at_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    db = _db(context)
    students = await db.get_students_with_telegram()
    sent = 0
    for s in students:
        try:
            await context.bot.send_message(
                chat_id=s["telegram_id"],
                text=f"📢 *Сообщение от преподавателя:*\n\n{text}",
                parse_mode=ParseMode.MARKDOWN,
            )
            sent += 1
        except Exception:
            pass
    context.user_data["admin_state"] = ST_NONE
    await update.message.reply_text(f"✅ Отправлено {sent} студентам.")


# ---------------------------------------------------------------------------
# Section: prompts
# ---------------------------------------------------------------------------

async def _show_prompts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    roles = await _db(context).get_roles()
    kb_rows = [
        [InlineKeyboardButton(f"✏️ {r['display_name']}", callback_data=f"adm:edit_prompt:{r['id']}")]
        for r in roles
    ]
    kb_rows.extend(kb_back())
    await update.callback_query.edit_message_text(
        "✏️ *Промпты агентов*\n\nВыберите роль для редактирования:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def _edit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, role_id: int) -> None:
    role = await _db(context).get_role_by_id(role_id)
    prompt = await _db(context).get_active_prompt(role_id)
    context.user_data["admin_state"] = ST_EDIT_PROMPT_TEXT
    context.user_data["admin_temp"] = {"role_id": role_id, "role_name": role["display_name"]}

    current = prompt["system_prompt"] if prompt else "(нет промпта)"
    version = prompt["version"] if prompt else 0
    await update.callback_query.edit_message_text(
        f"✏️ *{role['display_name']}* — версия {version}\n\n"
        f"Текущий промпт:\n```\n{current[:1000]}\n```\n\n"
        "Введите новый текст промпта (или отправьте тот же для подтверждения):",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_back("adm:prompts")),
    )


async def _at_prompt_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    temp = context.user_data["admin_temp"]
    tg_user = str(update.effective_user.id)
    new_id = await _db(context).update_prompt(temp["role_id"], text, tg_user)
    context.user_data["admin_state"] = ST_NONE
    context.user_data["admin_temp"] = {}
    await update.message.reply_text(
        f"✅ Промпт для *{temp['role_name']}* обновлён (ID версии: {new_id}).\n"
        "Изменения применяются немедленно для новых сообщений.",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Section: analytics
# ---------------------------------------------------------------------------

async def _show_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    team_stats = await db.get_team_stats()
    iter_stats = await db.get_iteration_stats()

    lines = ["📊 *Статистика по командам:*\n"]
    for t in team_stats:
        last = t["last_activity"][:16].replace("T", " ") if t["last_activity"] else "нет"
        lines.append(
            f"*{t['team_name']}* ({t['group_name']})\n"
            f"  Сообщений студентов: {t['student_messages']}, агента: {t['agent_messages']}\n"
            f"  Использовано ответов: {t['labeled_used']}\n"
            f"  Ср. длина сообщения: {t['avg_msg_len'] or 0} симв.\n"
            f"  Последняя активность: {last}"
        )

    lines.append("\n📈 *По итерациям:*")
    for i in iter_stats:
        active_mark = " ◀️ активна" if i["is_active"] else ""
        lines.append(
            f"*{i['name']}*{active_mark}: {i['student_messages']} сообщ., {i['active_students']} студентов"
        )

    text = "\n\n".join(lines)
    chunks = [text[i:i+3800] for i in range(0, len(text), 3800)]
    msg = update.callback_query.message
    await update.callback_query.edit_message_text(
        chunks[0],
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(kb_back()),
    )
    for chunk in chunks[1:]:
        await msg.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Section: export
# ---------------------------------------------------------------------------

async def _show_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Все диалоги (CSV)", callback_data="adm:export:messages_all")],
        [InlineKeyboardButton("📊 Статистика команд (CSV)", callback_data="adm:export:team_stats")],
        [InlineKeyboardButton("📋 Рефлексии (CSV)", callback_data="adm:export:reflections_all")],
        *kb_back(),
    ])
    await update.callback_query.edit_message_text(
        "📤 *Экспорт данных*\n\nВыберите тип экспорта:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def _do_export(update: Update, context: ContextTypes.DEFAULT_TYPE, export_type: str) -> None:
    exp = _exp(context)
    await update.callback_query.answer("Готовлю файл...")

    try:
        if export_type == "messages_all":
            data = await exp.export_all_messages()
            filename = f"dialogs_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
        elif export_type == "team_stats":
            data = await exp.export_team_stats()
            filename = f"team_stats_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
        elif export_type == "reflections_all":
            data = await exp.export_reflections()
            filename = f"reflections_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"
        else:
            await update.callback_query.message.reply_text("Неизвестный тип экспорта.")
            return

        await update.callback_query.message.reply_document(
            document=InputFile(io.BytesIO(data), filename=filename),
            caption=f"📎 {filename}",
        )
    except Exception as e:
        log.error("Export error: %s", e)
        await update.callback_query.message.reply_text(f"❌ Ошибка экспорта: {e}")


# ---------------------------------------------------------------------------
# Section: monitor
# ---------------------------------------------------------------------------

async def _show_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    recent = await db.get_recent_activity(60)
    inactive = await db.get_inactive_students(20)

    iteration = await db.get_active_iteration()
    iter_name = iteration["name"] if iteration else "не задана"

    lines = [f"🔴 *Мониторинг* | {iter_name}\n"]
    if recent:
        lines.append("*Активность за последний час:*")
        for r in recent[:15]:
            ts = r["last_msg"][:16].replace("T", " ")
            lines.append(f"• {r['student_name']} ({r['team_name']}) — {ts}")
    else:
        lines.append("Активности за последний час нет.")

    if inactive:
        lines.append(f"\n⚠️ *Не активны более 20 минут ({len(inactive)} чел.):*")
        for s in inactive[:10]:
            last = s["last_msg"][:16].replace("T", " ") if s["last_msg"] else "никогда"
            lines.append(f"• {s['name']} ({s['team_name']}) — посл.: {last}")

    await update.callback_query.edit_message_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Обновить", callback_data="adm:monitor")],
            *kb_back(),
        ]),
    )


# ---------------------------------------------------------------------------
# Assign team role inline
# ---------------------------------------------------------------------------

async def _assign_team_role(
    update: Update, context: ContextTypes.DEFAULT_TYPE, team_id: int, role_id: int
) -> None:
    await _db(context).update_team_role(team_id, role_id)
    await update.callback_query.answer("Роль назначена.")
    await _list_teams(update, context)


# ---------------------------------------------------------------------------
# AI-heavy: add / remove agents
# ---------------------------------------------------------------------------

async def _add_team_agent(
    update: Update, context: ContextTypes.DEFAULT_TYPE, team_id: int, role_id: int
) -> None:
    await _db(context).add_team_agent(team_id, role_id)
    await update.callback_query.answer("Агент добавлен.")
    await _manage_team_agents(update, context, team_id)


async def _remove_team_agent(
    update: Update, context: ContextTypes.DEFAULT_TYPE, team_id: int, role_id: int
) -> None:
    await _db(context).remove_team_agent(team_id, role_id)
    await update.callback_query.answer("Агент удалён.")
    await _manage_team_agents(update, context, team_id)


# ---------------------------------------------------------------------------
# Register handlers
# ---------------------------------------------------------------------------

def build_admin_handlers() -> list:
    return [
        CommandHandler("admin", cmd_admin),
        CallbackQueryHandler(handle_admin_callback, pattern=r"^adm:"),
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_admin_text,
        ),
    ]
