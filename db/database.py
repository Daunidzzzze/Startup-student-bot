"""
Database layer — SQLite via aiosqlite.
All public methods are async and return plain dicts or lists of dicts.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import aiosqlite

from config import DEFAULT_ROLES, DEFAULT_ROLES_TEAM_MEMBER

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS agent_roles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    role_id        INTEGER NOT NULL REFERENCES agent_roles(id),
    version        INTEGER NOT NULL DEFAULT 1,
    system_prompt  TEXT NOT NULL,
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    created_by     TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    code         TEXT NOT NULL UNIQUE,
    group_name   TEXT NOT NULL,
    role_id      INTEGER REFERENCES agent_roles(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS students (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER UNIQUE,
    name        TEXT NOT NULL,
    team_id     INTEGER NOT NULL REFERENCES teams(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS iterations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    number      INTEGER NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 0,
    agent_enabled INTEGER NOT NULL DEFAULT 1,
    started_at  TEXT,
    ended_at    TEXT
);

CREATE TABLE IF NOT EXISTS team_agents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id     INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    role_id     INTEGER NOT NULL REFERENCES agent_roles(id),
    position    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(team_id, role_id)
);

CREATE TABLE IF NOT EXISTS messages (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id         INTEGER NOT NULL REFERENCES students(id),
    team_id            INTEGER NOT NULL REFERENCES teams(id),
    iteration_id       INTEGER NOT NULL REFERENCES iterations(id),
    role               TEXT NOT NULL,
    agent_name         TEXT,
    content            TEXT NOT NULL,
    sequence_number    INTEGER NOT NULL DEFAULT 0,
    prompt_version_id  INTEGER REFERENCES prompt_versions(id),
    latency_ms         INTEGER,
    round_number       INTEGER NOT NULL DEFAULT 1,
    timestamp          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS message_labels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL REFERENCES messages(id),
    student_id  INTEGER NOT NULL REFERENCES students(id),
    label       TEXT NOT NULL,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reflections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id          INTEGER NOT NULL REFERENCES students(id),
    team_id             INTEGER NOT NULL REFERENCES teams(id),
    iteration_id        INTEGER NOT NULL REFERENCES iterations(id),
    contribution_score  INTEGER,
    cognitive_load      INTEGER,
    agent_usefulness    INTEGER,
    timestamp           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS session_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER REFERENCES students(id),
    event_type  TEXT NOT NULL,
    description TEXT,
    metadata    TEXT,
    timestamp   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SEED_ITERATIONS = """
INSERT OR IGNORE INTO iterations (number, name, is_active) VALUES
    (1, 'Занятие 1', 1),
    (2, 'Занятие 2', 0),
    (3, 'Занятие 3', 0);
"""


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.executescript(SEED_ITERATIONS)
        await self._seed_default_roles()
        await self._migrate()
        await self._db.commit()

    async def _migrate(self) -> None:
        """Safe migrations for columns added after initial release."""
        for stmt in (
            "ALTER TABLE messages ADD COLUMN agent_name TEXT",
            "ALTER TABLE messages ADD COLUMN round_number INTEGER NOT NULL DEFAULT 1",
        ):
            try:
                await self._db.execute(stmt)
                await self._db.commit()
            except Exception:
                pass  # column already exists

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # Signature that marks old coaching-style prompts eligible for auto-update
    _OLD_PROMPT_SNIPPETS = (
        "задавай уточняющие вопросы",
        "Не выполняй работу за студентов",
        "Не даёт готовых решений",
        "только формулируешь проблемы",
    )

    async def _seed_default_roles(self) -> None:
        for name, data in DEFAULT_ROLES.items():
            async with self._db.execute(
                "SELECT id FROM agent_roles WHERE name = ?", (name,)
            ) as cur:
                row = await cur.fetchone()

            if row is None:
                # Brand-new install — create role + H+AI prompt + team-member prompt
                await self._db.execute(
                    "INSERT INTO agent_roles (name, display_name) VALUES (?, ?)",
                    (name, data["display"]),
                )
                async with self._db.execute(
                    "SELECT id FROM agent_roles WHERE name = ?", (name,)
                ) as cur:
                    role_row = await cur.fetchone()
                role_id = role_row["id"]
                # v1 — H+AI guide prompt
                await self._db.execute(
                    "INSERT INTO prompt_versions (role_id, version, system_prompt, created_by) "
                    "VALUES (?, 1, ?, 'system')",
                    (role_id, data["prompt"]),
                )
                # v2 — AI-heavy team-member prompt (active by default)
                tm_prompt = DEFAULT_ROLES_TEAM_MEMBER.get(name, data["prompt"])
                await self._db.execute(
                    "INSERT INTO prompt_versions (role_id, version, system_prompt, is_active, created_by) "
                    "VALUES (?, 2, ?, 1, 'system')",
                    (role_id, tm_prompt),
                )
                # Deactivate v1 so v2 is the active one
                await self._db.execute(
                    "UPDATE prompt_versions SET is_active = 0 "
                    "WHERE role_id = ? AND version = 1",
                    (role_id,),
                )
            else:
                role_id = row["id"]
                # Existing install — check if team-member prompt already exists
                async with self._db.execute(
                    "SELECT id, system_prompt FROM prompt_versions "
                    "WHERE role_id = ? ORDER BY version DESC LIMIT 1",
                    (role_id,),
                ) as cur:
                    latest = await cur.fetchone()
                tm_prompt = DEFAULT_ROLES_TEAM_MEMBER.get(name, data["prompt"])
                needs_update = (
                    latest is None
                    or "[team-member-v3]" not in latest["system_prompt"]
                    or any(s in latest["system_prompt"] for s in self._OLD_PROMPT_SNIPPETS)
                )
                if needs_update:
                    # Deactivate old, add new team-member version
                    await self._db.execute(
                        "UPDATE prompt_versions SET is_active = 0 WHERE role_id = ?",
                        (role_id,),
                    )
                    async with self._db.execute(
                        "SELECT MAX(version) as mv FROM prompt_versions WHERE role_id = ?",
                        (role_id,),
                    ) as cur:
                        mv_row = await cur.fetchone()
                    next_v = (mv_row["mv"] or 0) + 1
                    await self._db.execute(
                        "INSERT INTO prompt_versions "
                        "(role_id, version, system_prompt, is_active, created_by) "
                        "VALUES (?, ?, ?, 1, 'system-auto-update')",
                        (role_id, next_v, tm_prompt),
                    )

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._db.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        async with self._db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _execute(self, sql: str, params: tuple = ()) -> int:
        cur = await self._db.execute(sql, params)
        await self._db.commit()
        return cur.lastrowid

    # ------------------------------------------------------------------
    # Agent roles
    # ------------------------------------------------------------------

    async def get_roles(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM agent_roles ORDER BY name")

    async def get_role_by_name(self, name: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM agent_roles WHERE name = ?", (name,))

    async def get_role_by_id(self, role_id: int) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM agent_roles WHERE id = ?", (role_id,))

    # ------------------------------------------------------------------
    # Prompt versions
    # ------------------------------------------------------------------

    async def get_active_prompt(self, role_id: int) -> Optional[dict]:
        return await self._fetchone(
            "SELECT * FROM prompt_versions "
            "WHERE role_id = ? AND is_active = 1 "
            "ORDER BY version DESC LIMIT 1",
            (role_id,),
        )

    async def get_prompts_for_role(self, role_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM prompt_versions WHERE role_id = ? ORDER BY version DESC",
            (role_id,),
        )

    async def update_prompt(
        self, role_id: int, new_prompt: str, created_by: str
    ) -> int:
        """Deactivate old, insert new version. Returns new prompt_version id."""
        async with self._db.execute(
            "SELECT MAX(version) as mv FROM prompt_versions WHERE role_id = ?",
            (role_id,),
        ) as cur:
            row = await cur.fetchone()
        next_version = (row["mv"] or 0) + 1
        await self._db.execute(
            "UPDATE prompt_versions SET is_active = 0 WHERE role_id = ?", (role_id,)
        )
        pid = await self._execute(
            "INSERT INTO prompt_versions (role_id, version, system_prompt, created_by) "
            "VALUES (?, ?, ?, ?)",
            (role_id, next_version, new_prompt, created_by),
        )
        return pid

    async def add_role(self, name: str, display_name: str, prompt: str, created_by: str) -> int:
        role_id = await self._execute(
            "INSERT INTO agent_roles (name, display_name) VALUES (?, ?)",
            (name, display_name),
        )
        await self._execute(
            "INSERT INTO prompt_versions (role_id, version, system_prompt, created_by) "
            "VALUES (?, 1, ?, ?)",
            (role_id, prompt, created_by),
        )
        return role_id

    # ------------------------------------------------------------------
    # Teams
    # ------------------------------------------------------------------

    async def create_team(
        self, name: str, code: str, group_name: str, role_id: Optional[int]
    ) -> int:
        return await self._execute(
            "INSERT INTO teams (name, code, group_name, role_id) VALUES (?, ?, ?, ?)",
            (name, code, group_name, role_id),
        )

    async def get_team_by_code(self, code: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM teams WHERE code = ?", (code,))

    async def get_team_by_id(self, team_id: int) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM teams WHERE id = ?", (team_id,))

    async def get_all_teams(self) -> list[dict]:
        return await self._fetchall(
            "SELECT t.*, r.display_name as role_display "
            "FROM teams t LEFT JOIN agent_roles r ON t.role_id = r.id "
            "ORDER BY t.group_name, t.name"
        )

    async def update_team_role(self, team_id: int, role_id: int) -> None:
        await self._execute("UPDATE teams SET role_id = ? WHERE id = ?", (role_id, team_id))

    async def delete_team(self, team_id: int) -> None:
        await self._execute("DELETE FROM teams WHERE id = ?", (team_id,))

    # ------------------------------------------------------------------
    # Team agents (AI-heavy: multiple roles per team)
    # ------------------------------------------------------------------

    async def add_team_agent(self, team_id: int, role_id: int) -> None:
        """Assign an agent role to an AI-heavy team."""
        async with self._db.execute(
            "SELECT MAX(position) as mp FROM team_agents WHERE team_id = ?", (team_id,)
        ) as cur:
            row = await cur.fetchone()
        pos = (row["mp"] or 0) + 1
        try:
            await self._execute(
                "INSERT INTO team_agents (team_id, role_id, position) VALUES (?, ?, ?)",
                (team_id, role_id, pos),
            )
        except Exception:
            pass  # already exists — ignore duplicate

    async def remove_team_agent(self, team_id: int, role_id: int) -> None:
        await self._execute(
            "DELETE FROM team_agents WHERE team_id = ? AND role_id = ?", (team_id, role_id)
        )

    async def get_team_agents(self, team_id: int) -> list[dict]:
        """Return list of {role, prompt} dicts ordered by position."""
        roles = await self._fetchall(
            "SELECT ar.id, ar.name, ar.display_name, ta.position "
            "FROM team_agents ta "
            "JOIN agent_roles ar ON ta.role_id = ar.id "
            "WHERE ta.team_id = ? ORDER BY ta.position",
            (team_id,),
        )
        result = []
        for r in roles:
            prompt = await self.get_active_prompt(r["id"])
            result.append({"role": dict(r), "prompt": prompt})
        return result

    # ------------------------------------------------------------------
    # Students
    # ------------------------------------------------------------------

    async def create_student(
        self, name: str, team_id: int, telegram_id: Optional[int] = None
    ) -> int:
        return await self._execute(
            "INSERT INTO students (name, team_id, telegram_id) VALUES (?, ?, ?)",
            (name, team_id, telegram_id),
        )

    async def get_student_by_telegram(self, telegram_id: int) -> Optional[dict]:
        return await self._fetchone(
            "SELECT s.*, t.group_name, t.role_id, t.name as team_name "
            "FROM students s JOIN teams t ON s.team_id = t.id "
            "WHERE s.telegram_id = ?",
            (telegram_id,),
        )

    async def get_students_by_team(self, team_id: int) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM students WHERE team_id = ? ORDER BY name", (team_id,)
        )

    async def get_all_students(self) -> list[dict]:
        return await self._fetchall(
            "SELECT s.*, t.name as team_name, t.group_name "
            "FROM students s JOIN teams t ON s.team_id = t.id "
            "ORDER BY t.group_name, t.name, s.name"
        )

    async def link_telegram(self, student_id: int, telegram_id: int) -> None:
        await self._execute(
            "UPDATE students SET telegram_id = ? WHERE id = ?", (telegram_id, student_id)
        )

    async def get_students_with_telegram(self) -> list[dict]:
        """All students who have a registered telegram_id."""
        return await self._fetchall(
            "SELECT s.*, t.group_name FROM students s "
            "JOIN teams t ON s.team_id = t.id "
            "WHERE s.telegram_id IS NOT NULL"
        )

    async def delete_student(self, student_id: int) -> None:
        await self._execute("DELETE FROM students WHERE id = ?", (student_id,))

    # ------------------------------------------------------------------
    # Iterations
    # ------------------------------------------------------------------

    async def get_active_iteration(self) -> Optional[dict]:
        return await self._fetchone(
            "SELECT * FROM iterations WHERE is_active = 1 LIMIT 1"
        )

    async def get_all_iterations(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM iterations ORDER BY number")

    async def set_active_iteration(self, iteration_number: int) -> None:
        now = datetime.utcnow().isoformat()
        await self._db.execute("UPDATE iterations SET is_active = 0, ended_at = ?", (now,))
        await self._db.execute(
            "UPDATE iterations SET is_active = 1, started_at = ?, ended_at = NULL "
            "WHERE number = ?",
            (now, iteration_number),
        )
        await self._db.commit()

    async def set_agent_enabled(self, enabled: bool, team_id: Optional[int] = None) -> None:
        """Enable/disable agent globally or for a specific team's iteration state."""
        val = 1 if enabled else 0
        if team_id is None:
            await self._execute("UPDATE iterations SET agent_enabled = ? WHERE is_active = 1", (val,))
        else:
            # Per-team override stored in session_events; actual check done in handler
            event = "agent_enabled" if enabled else "agent_disabled"
            await self._execute(
                "INSERT INTO session_events (event_type, description, metadata) VALUES (?, ?, ?)",
                (event, f"Team {team_id}", json.dumps({"team_id": team_id})),
            )

    async def is_agent_enabled_for_team(self, team_id: int) -> bool:
        """Check if agent is currently enabled (global iteration setting,
        with per-team override from session_events)."""
        iteration = await self.get_active_iteration()
        if not iteration or not iteration["agent_enabled"]:
            return False
        # Check per-team disable event (most recent event wins)
        row = await self._fetchone(
            "SELECT event_type FROM session_events "
            "WHERE (event_type = 'agent_enabled' OR event_type = 'agent_disabled') "
            "AND metadata LIKE ? "
            "ORDER BY timestamp DESC LIMIT 1",
            (f'%"team_id": {team_id}%',),
        )
        if row:
            return row["event_type"] == "agent_enabled"
        return True

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    async def save_message(
        self,
        student_id: int,
        team_id: int,
        iteration_id: int,
        role: str,
        content: str,
        agent_name: Optional[str] = None,
        prompt_version_id: Optional[int] = None,
        latency_ms: Optional[int] = None,
        round_number: int = 1,
    ) -> int:
        async with self._db.execute(
            "SELECT COUNT(*) as cnt FROM messages "
            "WHERE student_id = ? AND iteration_id = ?",
            (student_id, iteration_id),
        ) as cur:
            row = await cur.fetchone()
        seq = row["cnt"] + 1
        return await self._execute(
            "INSERT INTO messages "
            "(student_id, team_id, iteration_id, role, agent_name, content, sequence_number, "
            "prompt_version_id, latency_ms, round_number) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (student_id, team_id, iteration_id, role, agent_name, content, seq,
             prompt_version_id, latency_ms, round_number),
        )

    async def get_messages_for_context(
        self, student_id: int, iteration_id: int, limit: int = 20
    ) -> list[dict]:
        """Last N messages for building AI context."""
        rows = await self._fetchall(
            "SELECT * FROM messages "
            "WHERE student_id = ? AND iteration_id = ? "
            "ORDER BY sequence_number DESC LIMIT ?",
            (student_id, iteration_id, limit),
        )
        return list(reversed(rows))

    async def get_messages_for_student(
        self, student_id: int, iteration_id: Optional[int] = None
    ) -> list[dict]:
        if iteration_id:
            return await self._fetchall(
                "SELECT m.*, ml.label FROM messages m "
                "LEFT JOIN message_labels ml ON ml.message_id = m.id AND ml.student_id = m.student_id "
                "WHERE m.student_id = ? AND m.iteration_id = ? ORDER BY m.sequence_number",
                (student_id, iteration_id),
            )
        return await self._fetchall(
            "SELECT m.*, ml.label FROM messages m "
            "LEFT JOIN message_labels ml ON ml.message_id = m.id AND ml.student_id = m.student_id "
            "WHERE m.student_id = ? ORDER BY m.iteration_id, m.sequence_number",
            (student_id,),
        )

    async def get_messages_for_team(
        self, team_id: int, iteration_id: Optional[int] = None
    ) -> list[dict]:
        if iteration_id:
            return await self._fetchall(
                "SELECT m.*, s.name as student_name, ml.label "
                "FROM messages m "
                "JOIN students s ON m.student_id = s.id "
                "LEFT JOIN message_labels ml ON ml.message_id = m.id "
                "WHERE m.team_id = ? AND m.iteration_id = ? "
                "ORDER BY m.timestamp",
                (team_id, iteration_id),
            )
        return await self._fetchall(
            "SELECT m.*, s.name as student_name, ml.label "
            "FROM messages m "
            "JOIN students s ON m.student_id = s.id "
            "LEFT JOIN message_labels ml ON ml.message_id = m.id "
            "WHERE m.team_id = ? ORDER BY m.iteration_id, m.timestamp",
            (team_id,),
        )

    async def get_all_messages(self) -> list[dict]:
        return await self._fetchall(
            "SELECT m.*, s.name as student_name, t.name as team_name, "
            "t.group_name, i.name as iteration_name, ml.label "
            "FROM messages m "
            "JOIN students s ON m.student_id = s.id "
            "JOIN teams t ON m.team_id = t.id "
            "JOIN iterations i ON m.iteration_id = i.id "
            "LEFT JOIN message_labels ml ON ml.message_id = m.id "
            "ORDER BY m.timestamp"
        )

    # ------------------------------------------------------------------
    # Labels
    # ------------------------------------------------------------------

    async def save_label(self, message_id: int, student_id: int, label: str) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO message_labels (message_id, student_id, label) "
            "VALUES (?, ?, ?)",
            (message_id, student_id, label),
        )

    # ------------------------------------------------------------------
    # Reflections
    # ------------------------------------------------------------------

    async def save_reflection(
        self,
        student_id: int,
        team_id: int,
        iteration_id: int,
        contribution: int,
        cognitive_load: int,
        agent_usefulness: Optional[int],
    ) -> int:
        return await self._execute(
            "INSERT INTO reflections "
            "(student_id, team_id, iteration_id, contribution_score, "
            "cognitive_load, agent_usefulness) VALUES (?, ?, ?, ?, ?, ?)",
            (student_id, team_id, iteration_id, contribution, cognitive_load, agent_usefulness),
        )

    async def get_all_reflections(self) -> list[dict]:
        return await self._fetchall(
            "SELECT r.*, s.name as student_name, t.name as team_name, "
            "t.group_name, i.name as iteration_name "
            "FROM reflections r "
            "JOIN students s ON r.student_id = s.id "
            "JOIN teams t ON r.team_id = t.id "
            "JOIN iterations i ON r.iteration_id = i.id "
            "ORDER BY r.timestamp"
        )

    # ------------------------------------------------------------------
    # Session events
    # ------------------------------------------------------------------

    async def log_event(
        self,
        event_type: str,
        description: str = "",
        student_id: Optional[int] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        await self._execute(
            "INSERT INTO session_events (student_id, event_type, description, metadata) "
            "VALUES (?, ?, ?, ?)",
            (student_id, event_type, description, json.dumps(metadata) if metadata else None),
        )

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    async def get_team_stats(self) -> list[dict]:
        return await self._fetchall(
            """
            SELECT
                t.id as team_id,
                t.name as team_name,
                t.group_name,
                COUNT(DISTINCT m.student_id) as active_students,
                COUNT(CASE WHEN m.role = 'student' THEN 1 END) as student_messages,
                COUNT(CASE WHEN m.role = 'agent' THEN 1 END) as agent_messages,
                COUNT(ml.id) as labeled_used,
                ROUND(AVG(CASE WHEN m.role = 'student' THEN LENGTH(m.content) END), 1) as avg_msg_len,
                MAX(m.timestamp) as last_activity
            FROM teams t
            LEFT JOIN messages m ON m.team_id = t.id
            LEFT JOIN message_labels ml ON ml.message_id = m.id AND ml.label = 'used'
            GROUP BY t.id
            ORDER BY t.group_name, t.name
            """
        )

    async def get_iteration_stats(self) -> list[dict]:
        return await self._fetchall(
            """
            SELECT
                i.number, i.name, i.is_active, i.agent_enabled,
                COUNT(CASE WHEN m.role = 'student' THEN 1 END) as student_messages,
                COUNT(DISTINCT m.student_id) as active_students
            FROM iterations i
            LEFT JOIN messages m ON m.iteration_id = i.id
            GROUP BY i.id
            ORDER BY i.number
            """
        )

    async def get_recent_activity(self, minutes: int = 60) -> list[dict]:
        return await self._fetchall(
            "SELECT s.name as student_name, t.name as team_name, "
            "MAX(m.timestamp) as last_msg "
            "FROM messages m "
            "JOIN students s ON m.student_id = s.id "
            "JOIN teams t ON m.team_id = t.id "
            "WHERE m.timestamp >= datetime('now', ? || ' minutes') "
            "GROUP BY m.student_id ORDER BY last_msg DESC",
            (f"-{minutes}",),
        )

    async def get_inactive_students(self, minutes: int = 20) -> list[dict]:
        """Students in AI groups who haven't sent a message in N minutes."""
        return await self._fetchall(
            "SELECT s.telegram_id, s.name, t.name as team_name, "
            "MAX(m.timestamp) as last_msg "
            "FROM students s "
            "JOIN teams t ON s.team_id = t.id "
            "LEFT JOIN messages m ON m.student_id = s.id "
            "WHERE t.group_name != 'H' AND s.telegram_id IS NOT NULL "
            "GROUP BY s.id "
            "HAVING last_msg IS NULL OR last_msg < datetime('now', ? || ' minutes')",
            (f"-{minutes}",),
        )
