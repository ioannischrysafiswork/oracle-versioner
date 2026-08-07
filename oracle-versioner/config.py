"""
config.py
Loads application settings from config.yaml.
"""
import yaml
from pathlib import Path
def load_config(config_file="config/config.yaml"):
    """Load configuration and return it as a dictionary."""
    # Create a Path object for the config file.
    config_path = Path(config_file)
    # Verify the file exists.
    if not config_path.exists():
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}"
        )
    # Open the YAML file for reading.
    with open(config_path, "r", encoding="utf-8") as file:
        # Convert YAML into a Python dictionary.
        config = yaml.safe_load(file)
    # Return the loaded configuration.
    return config