"""
Multi-agent service for AI-heavy teams.

Логика:
  1. Оркестратор (быстрый LLM-вызов) смотрит на последние сообщения и решает,
     КОМУ из агентов нужно ответить — или никому (NONE).
  2. Выбранный агент отвечает с доступом к инструменту поиска в интернете
     (DuckDuckGo через tool_calling). Остальные агенты молчат.
  3. Все агенты разделяют общий контекст — видят переписку с именами коллег.

AgentResponse.search_used == True → агент использовал веб-поиск.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from openai import AsyncOpenAI

from config import OPENAI_MODEL

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool definition для OpenAI function-calling
# ---------------------------------------------------------------------------

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": (
            "Поиск актуальной информации в интернете. "
            "Используй для получения данных о рынках, компаниях, технологиях, "
            "статистики, новостей и любой другой актуальной информации, "
            "которой нет в твоих обучающих данных."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос (можно на русском или английском)",
                }
            },
            "required": ["query"],
        },
    },
}

# ---------------------------------------------------------------------------
# Orchestrator prompt
# ---------------------------------------------------------------------------

ORCHESTRATOR_SYSTEM = """\
Ты — оркестратор мультиагентной команды стартап-проекта.
В команде работают следующие ИИ-агенты:

{agents_description}

Твоя задача: определить, КАКОЙ агент должен ответить на сообщение студента.

ГЛАВНОЕ ПРАВИЛО: агент должен отвечать на ЛЮБОЙ содержательный запрос студента.
Студенты работают над стартап-проектом — почти всё, что они пишут, требует ответа.

КОГДА отвечать NONE (только эти случаи):
- Сообщение состоит только из «ок», «понял», «ясно», «спасибо», «хорошо», «👍» — без вопроса.
- Студент прощается: «пока», «до встречи».

ВО ВСЕХ ОСТАЛЬНЫХ СЛУЧАЯХ выбирай наиболее подходящего агента:
- Аналитик — данные, исследования, структуризация, цифры, рынок, конкуренты.
- Стратег — что делать дальше, план, направления, решения, бизнес-модель.
- Критик — проверка идеи, риски, слабые места, обратная связь по готовому тексту.
Если агент с другим названием — используй здравый смысл по его описанию.

Отвечай ТОЛЬКО именем агента (точно как написано выше) или словом NONE.
Никаких объяснений.\
"""

# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass
class AgentResponse:
    role_id: int
    role_name: str
    content: str
    latency_ms: int
    prompt_version_id: Optional[int]
    round_number: int = 1
    search_used: bool = False
    search_queries: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class MultiAgentService:
    def __init__(self, api_key: str, model: str = OPENAI_MODEL) -> None:
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def route_and_respond(
        self,
        agents: list[dict],
        history: list[dict],
        student_message: str,
        on_searching: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> Optional[AgentResponse]:
        """
        Выбирает агента-ответчика и получает ответ (с возможным поиском).
        Возвращает None если ни один агент не должен отвечать.
        """
        chosen = await self._route(agents, history, student_message)
        if chosen is None:
            log.info("Orchestrator: NONE — no agent responds")
            return None

        log.info("Orchestrator: chosen agent = %s", chosen["role"]["display_name"])
        # Pass names of other agents so the chosen one knows its teammates
        other_names = [a["role"]["display_name"] for a in agents
                       if a["role"]["id"] != chosen["role"]["id"]]
        return await self._respond(chosen, other_names, history, student_message, on_searching)

    # ------------------------------------------------------------------
    # Step 1: routing
    # ------------------------------------------------------------------

    async def _route(
        self, agents: list[dict], history: list[dict], message: str
    ) -> Optional[dict]:
        agents_desc = "\n".join(
            f"- {a['role']['display_name']}"
            for a in agents
        )

        recent = history[-8:]
        context_str = "\n".join(
            f"{'Студент' if m['role'] == 'student' else m.get('agent_name', 'Агент')}: "
            f"{m['content'][:250]}"
            for m in recent
        )

        try:
            resp = await self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {
                        "role": "system",
                        "content": ORCHESTRATOR_SYSTEM.format(
                            agents_description=agents_desc
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Последние сообщения:\n{context_str}\n\n"
                            f"Новое сообщение студента: {message}"
                        ),
                    },
                ],
                temperature=0,
                max_tokens=30,
            )
        except Exception as exc:
            log.error("Orchestrator error: %s", exc)
            return agents[0] if agents else None

        decision = resp.choices[0].message.content.strip()

        if decision.upper() == "NONE":
            return None

        # Exact match first
        for agent in agents:
            if agent["role"]["display_name"].strip().lower() == decision.lower():
                return agent

        # Partial match
        dec_lower = decision.lower()
        for agent in agents:
            if dec_lower in agent["role"]["display_name"].lower():
                return agent

        # Fallback: first agent
        log.warning("Orchestrator returned unknown name '%s', falling back to first agent", decision)
        return agents[0]

    # ------------------------------------------------------------------
    # Step 2: agent responds with optional web search
    # ------------------------------------------------------------------

    async def _respond(
        self,
        agent: dict,
        other_names: list[str],
        history: list[dict],
        student_message: str,
        on_searching: Optional[Callable],
    ) -> AgentResponse:
        base_prompt = agent["prompt"]["system_prompt"] if agent.get("prompt") else ""
        # Append teammate context so the agent knows who else is in the team
        if other_names:
            teammates = ", ".join(other_names)
            team_note = (
                f"\n\nТвои коллеги в команде: {teammates}. "
                "Ты видишь их сообщения в истории переписки (помечены [ИмяАгента]). "
                "Учитывай их работу — не дублируй, дополняй."
            )
            system_prompt = base_prompt + team_note
        else:
            system_prompt = base_prompt
        oai_history = self._format_history(history)
        messages = (
            [{"role": "system", "content": system_prompt}]
            + oai_history
            + [{"role": "user", "content": student_message}]
        )

        t0 = time.monotonic()
        search_used = False
        search_queries: list[str] = []

        # Only Analyst gets web search tool
        is_analyst = (
            "аналит" in agent["role"].get("display_name", "").lower()
            or agent["role"].get("name", "").lower() == "analyst"
        )

        # Agentic loop: analyst may call search_web multiple times
        while True:
            call_kwargs: dict = dict(
                model=self._model,
                messages=messages,
                temperature=0.7,
                max_tokens=1024,
            )
            if is_analyst:
                call_kwargs["tools"] = [WEB_SEARCH_TOOL]
                call_kwargs["tool_choice"] = "auto"

            resp = await self._client.chat.completions.create(**call_kwargs)

            choice = resp.choices[0]

            if choice.finish_reason != "tool_calls":
                # Final answer
                break

            # Process tool calls
            messages.append(choice.message)
            for tc in choice.message.tool_calls:
                if tc.function.name != "search_web":
                    continue
                try:
                    query = json.loads(tc.function.arguments)["query"]
                except (json.JSONDecodeError, KeyError):
                    query = tc.function.arguments

                search_queries.append(query)
                search_used = True

                if on_searching:
                    try:
                        await on_searching(agent["role"]["display_name"], query)
                    except Exception:
                        pass

                results = await _do_search(query)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": results,
                    }
                )

        latency = int((time.monotonic() - t0) * 1000)
        content = choice.message.content or ""

        return AgentResponse(
            role_id=agent["role"]["id"],
            role_name=agent["role"]["display_name"],
            content=content.strip(),
            latency_ms=latency,
            prompt_version_id=agent["prompt"]["id"] if agent.get("prompt") else None,
            round_number=1,
            search_used=search_used,
            search_queries=search_queries,
        )

    # ------------------------------------------------------------------
    # History formatting (shared across agents)
    # ------------------------------------------------------------------

    @staticmethod
    def _format_history(history: list[dict]) -> list[dict]:
        """
        Конвертирует внутреннюю историю в формат OpenAI.
        Несколько сообщений одной роли подряд объединяются.
        Агенты помечаются префиксом [ИмяАгента].
        """
        result: list[dict] = []
        buffer: list[str] = []
        cur_role: Optional[str] = None

        def flush() -> None:
            if buffer and cur_role:
                result.append({"role": cur_role, "content": "\n\n".join(buffer)})

        for msg in history:
            if msg["role"] == "student":
                oai_role, text = "user", msg["content"]
            else:
                oai_role = "assistant"
                name = msg.get("agent_name") or "Агент"
                text = f"[{name}]: {msg['content']}"

            if oai_role == cur_role:
                buffer.append(text)
            else:
                flush()
                buffer, cur_role = [text], oai_role

        flush()
        return result


# ---------------------------------------------------------------------------
# Web search helper (DuckDuckGo, no API key needed)
# ---------------------------------------------------------------------------


async def _do_search(query: str, max_results: int = 4) -> str:
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=max_results))
        if not hits:
            return "Поиск не дал результатов по данному запросу."
        parts = []
        for h in hits:
            parts.append(
                f"Источник: {h.get('href', '')}\n"
                f"Заголовок: {h.get('title', '')}\n"
                f"Содержание: {h.get('body', '')}"
            )
        return "\n\n---\n\n".join(parts)
    except ImportError:
        return "Поиск недоступен: установите пакет duckduckgo-search."
    except Exception as exc:
        log.warning("Search error for query '%s': %s", query, exc)
        return f"Не удалось выполнить поиск: {exc}"
