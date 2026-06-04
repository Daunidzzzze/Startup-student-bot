"""
CSV export utilities — produces pandas DataFrames and saves to bytes.
All DataFrames are suitable for direct import into Excel / SPSS / R.
"""
from __future__ import annotations

import io
from typing import Optional

import pandas as pd

from db.database import Database


class ExportService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Public export methods (all return bytes ready to send as file)
    # ------------------------------------------------------------------

    async def export_all_messages(
        self,
        team_id: Optional[int] = None,
        group_name: Optional[str] = None,
        iteration_id: Optional[int] = None,
    ) -> bytes:
        rows = await self._db.get_all_messages()
        df = pd.DataFrame(rows)
        if df.empty:
            df = pd.DataFrame(columns=[
                "id", "student_name", "team_name", "group_name",
                "iteration_name", "role", "content", "sequence_number",
                "latency_ms", "label", "timestamp",
            ])
        else:
            if team_id:
                df = df[df["team_id"] == team_id]
            if group_name:
                df = df[df["group_name"] == group_name]
            if iteration_id:
                df = df[df["iteration_id"] == iteration_id]
            keep = [
                "id", "student_name", "team_name", "group_name",
                "iteration_name", "role", "content", "sequence_number",
                "latency_ms", "label", "timestamp",
            ]
            df = df[[c for c in keep if c in df.columns]]
        return _df_to_csv(df)

    async def export_team_stats(self) -> bytes:
        rows = await self._db.get_team_stats()
        df = pd.DataFrame(rows) if rows else pd.DataFrame(columns=[
            "team_id", "team_name", "group_name", "active_students",
            "student_messages", "agent_messages", "labeled_used",
            "avg_msg_len", "last_activity",
        ])
        return _df_to_csv(df)

    async def export_reflections(
        self,
        team_id: Optional[int] = None,
        group_name: Optional[str] = None,
    ) -> bytes:
        rows = await self._db.get_all_reflections()
        df = pd.DataFrame(rows)
        if df.empty:
            df = pd.DataFrame(columns=[
                "id", "student_name", "team_name", "group_name",
                "iteration_name", "contribution_score",
                "cognitive_load", "agent_usefulness", "timestamp",
            ])
        else:
            if team_id:
                df = df[df["team_id"] == team_id]
            if group_name:
                df = df[df["group_name"] == group_name]
            keep = [
                "id", "student_name", "team_name", "group_name",
                "iteration_name", "contribution_score",
                "cognitive_load", "agent_usefulness", "timestamp",
            ]
            df = df[[c for c in keep if c in df.columns]]
        return _df_to_csv(df)


def _df_to_csv(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_csv(buf, index=False, encoding="utf-8-sig")  # utf-8-sig for Excel BOM
    return buf.getvalue()
