"""Oracle access layer — the only layer that touches a database.

Strictly read-only (CLAUDE.md §5): dictionary ``SELECT``s, read-only
``DBMS_METADATA`` calls, and session-scoped ``ALTER SESSION`` (decision D-2).
Nothing here may ever emit DML or DDL against a source database.
"""
