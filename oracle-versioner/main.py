"""
main.py
Entry point for the Oracle DDL Versioning Tool.
"""
import logging
from config import load_config
from db import Database
from object_discovery import ObjectDiscovery
from ddl_exporter import DDLExporter
def main():
    """Main application workflow."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )
    logging.info("Starting Oracle DDL Versioning Tool...")
    # Load configuration
    config = load_config()
    # Connect to Oracle
    db = Database(config)
    db.connect()
    try:
        # Discover database objects
        discovery = ObjectDiscovery(db)
        objects = discovery.get_objects()
        logging.info("Found %d objects.", len(objects))
        # Export DDL
        exporter = DDLExporter(db, config)
        for obj in objects:
            logging.info(
                "Exporting %s %s.%s",
                obj["object_type"],
                obj["owner"],
                obj["object_name"]
            )
            exporter.export(obj)
        logging.info("Export completed successfully.")
    finally:
        db.disconnect()
        logging.info("Disconnected from Oracle.")
if __name__ == "__main__":
    main()