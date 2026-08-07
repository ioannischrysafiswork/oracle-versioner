"""
db.py
Handles all Oracle database operations.
"""
# Import the Oracle Python driver.
import oracledb
class Database:
    """Represents an Oracle database connection."""
    def __init__(self, config):
        """Initialize the Database object."""
        # Store the application's configuration.
        self.config = config
        # Initialize the Oracle connection.
        self.connection = None
        # Initialize the database cursor.
        self.cursor = None
    def connect(self):
        """Connect to the Oracle database."""
        # Read the connection settings from the configuration.
        connection = self.config["connection"]
        # Create a new Oracle connection.
        self.connection = oracledb.connect(
            user=connection["user"],
            password=connection["password"],
            host=connection["host"],
            port=connection["port"],
            service_name=connection["service"]
        )
        # Create a cursor for executing SQL statements.
        self.cursor = self.connection.cursor()
    def disconnect(self):
        """Close the Oracle connection."""
        # Close the cursor if it exists.
        if self.cursor:
            self.cursor.close()
        # Close the database connection if it exists.
        if self.connection:
            self.connection.close()
    def execute(self, sql, parameters=None):
        """Execute a SQL statement."""
        # Execute without bind variables.
        if parameters is None:
            self.cursor.execute(sql)
        # Execute using bind variables.
        else:
            self.cursor.execute(sql, parameters)
    def fetchone(self):
        """Return a single row from the last query."""
        return self.cursor.fetchone()
    def fetchall(self):
        """Return all rows from the last query."""
        return self.cursor.fetchall()
    def commit(self):
        """Commit the current transaction."""
        self.connection.commit()
    def rollback(self):
        """Rollback the current transaction."""
        self.connection.rollback()