"""Database access for the ArisMemory server, with the guardrails built in.

The AI never writes SQL. It can only reach the fixed, parameterized statements in this file, and each one
touches at most one row. Deletes are soft (the row gets a DeletedAt timestamp) and capped per session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import mssql_python

DEFAULT_MAX_DELETES = 5

_COLUMNS = "Id, Topic, Content, CreatedAt, UpdatedAt"


class MemoryStoreError(Exception):
    """An expected failure that should be shown to the model, such as an unknown id or a broken guardrail."""


@dataclass(frozen=True)
class MemoryRow:
    id: int
    topic: str
    content: str
    created_at: datetime
    updated_at: datetime | None


class MemoryStore:
    def __init__(self, connection_string: str, max_deletes: int = DEFAULT_MAX_DELETES) -> None:
        self._connection_string = connection_string
        self._max_deletes = max_deletes
        self._deletes_done = 0

    def _connect(self) -> mssql_python.Connection:
        # A fresh connection per call keeps things simple and survives SQL Server restarts.
        return mssql_python.connect(self._connection_string)

    def check_connection(self) -> None:
        """Fail early with a clear message if the database or table is missing."""
        conn = self._connect()
        try:
            conn.cursor().execute("SELECT TOP (0) Id FROM dbo.Memories")
        finally:
            conn.close()

    def add(self, topic: str, content: str) -> MemoryRow:
        return self._change_one_row(
            f"INSERT INTO dbo.Memories (Topic, Content) OUTPUT {_inserted()} VALUES (?, ?)",
            (topic, content),
        )

    def search(self, text: str = "", topic: str | None = None, limit: int = 20) -> list[MemoryRow]:
        pattern = f"%{_escape_like(text)}%"
        sql = (
            f"SELECT TOP (?) {_COLUMNS} FROM dbo.Memories "
            "WHERE DeletedAt IS NULL "
            "AND (? IS NULL OR Topic = ?) "
            "AND (Content LIKE ? ESCAPE '\\' OR Topic LIKE ? ESCAPE '\\') "
            "ORDER BY Id DESC"
        )
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, (limit, topic, topic, pattern, pattern))
            return [MemoryRow(*row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def update(self, memory_id: int, content: str, topic: str | None = None) -> MemoryRow:
        return self._change_one_row(
            "UPDATE dbo.Memories SET Content = ?, Topic = COALESCE(?, Topic), UpdatedAt = SYSUTCDATETIME() "
            f"OUTPUT {_inserted()} WHERE Id = ? AND DeletedAt IS NULL",
            (content, topic, memory_id),
        )

    def delete(self, memory_id: int) -> MemoryRow:
        if self._deletes_done >= self._max_deletes:
            raise MemoryStoreError(
                f"Delete limit reached: at most {self._max_deletes} deletions per session. "
                "Ask the user to remove further memories themselves."
            )
        row = self._change_one_row(
            f"UPDATE dbo.Memories SET DeletedAt = SYSUTCDATETIME() OUTPUT {_inserted()} "
            "WHERE Id = ? AND DeletedAt IS NULL",
            (memory_id,),
        )
        self._deletes_done += 1
        return row

    def _change_one_row(self, sql: str, params: tuple) -> MemoryRow:
        """Run one write in a transaction and commit only if exactly one row changed.

        The OUTPUT clause returns every row the statement touched. WHERE Id = ? on the primary key already
        limits each statement to one row; this check is the safety net if the SQL is ever changed.
        """
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            rows = cursor.fetchall()
            if not rows:
                raise MemoryStoreError("No active memory with that id. Use search_memories to find the right id.")
            if len(rows) > 1:
                raise MemoryStoreError(f"Refused: the change would affect {len(rows)} rows, only 1 is allowed.")
            conn.commit()
            return MemoryRow(*rows[0])
        except BaseException:
            conn.rollback()  # Nothing is saved unless we reached commit() above.
            raise
        finally:
            conn.close()


def _inserted() -> str:
    return ", ".join(f"INSERTED.{column.strip()}" for column in _COLUMNS.split(","))


def _escape_like(text: str) -> str:
    """Make %, _ and [ in the user's text match literally instead of acting as LIKE wildcards."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("[", "\\[")
