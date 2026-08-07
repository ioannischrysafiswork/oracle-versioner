"""
ddl_exporter.py

Exports Oracle object DDL using DBMS_METADATA.GET_DDL().
"""

class DDLExporter:
    """Exports Oracle object definitions."""

    def __init__(self, db):
        """Initialize the exporter."""

        # Store the database object.
        self.db = db

    def export(self, obj):
        """Export the DDL for a single Oracle object."""

        # SQL statement used to retrieve the object's DDL.
        sql = """
            SELECT DBMS_METADATA.GET_DDL(
                       :object_type,
                       :object_name,
                       :owner
                   )
            FROM dual
        """

        # Execute the SQL statement.
        self.db.execute(sql, {
            "object_type": obj["object_type"],
            "object_name": obj["object_name"],
            "owner": obj["owner"]
        })

        # Fetch the returned row.
        row = self.db.fetchone()

        # Return None if no DDL was found.
        if row is None:
            return None

        # Read the CLOB returned by Oracle.
        ddl = row[0].read()

        # Return the raw DDL.
        return ddl