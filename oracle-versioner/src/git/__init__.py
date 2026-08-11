"""Git integration via the ``git`` CLI (decision D-4 — no Git library).

Every operation in this package runs single-threaded, after all extraction has
finished: the working tree and the index are one shared mutable resource
(docs/architecture.md §1.3).
"""
