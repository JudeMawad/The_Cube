"""Read-only Sonarr access, ready for future TV conversations."""
from pathlib import Path

from .arr_client import ArrClient

CONFIG_PATH = Path.home() / ".config/cube/sonarr.env"


class SonarrClient(ArrClient):
    service = "SONARR"
    config_path = CONFIG_PATH
    resource = "series"
