"""
ddl_cleaner.py

Provides utility functions for normalizing Oracle DDL before it is
saved to disk.
"""

# Import Python's regular expression library.
import re


class DDLCleaner:
    """Normalizes DDL text."""

    # List of cleanup rules.
    #
    # Each rule consists of:
    #   (regular expression, replacement text)
    CLEANUP_RULES = [

        # Replace Windows line endings with Unix line endings.
        (r"\r\n", "\n"),

        # Replace old Mac line endings with Unix line endings.
        (r"\r", "\n"),

        # Remove trailing spaces and tabs at the end of lines.
        (r"[ \t]+$", ""),

        # Collapse three or more blank lines into two.
        (r"\n{3,}", "\n\n"),
    ]

    def clean(self, ddl):
        """Apply all cleanup rules."""

        # Remove leading and trailing whitespace.
        ddl = ddl.strip()

        # Apply each cleanup rule.
        for pattern, replacement in self.CLEANUP_RULES:
            ddl = re.sub(
                pattern,
                replacement,
                ddl,
                flags=re.MULTILINE
            )

        # Ensure the file ends with exactly one newline.
        return ddl + "\n"