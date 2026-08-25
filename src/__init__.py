"""Oracle DDL Versioning Agent.

Layering (docs/architecture.md §1.1); dependencies point strictly downward:

    ORCHESTRATION   src.main
    WORKFLOW        src.workflow
    DOMAIN          src.oracle | src.normalizer | src.repository | src.git | src.state
    FOUNDATION      src.config | src.models | src.logging | src.utils | src.exceptions
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
