from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from features.media import EventStore, MovieCommands, close_commands, event_router
from features.media.movie_commands import MovieCommands as ImplementationCommands
from features.media.event_routes import event_router as implementation_router
from integrations.overseerr_client import Movie


class MediaFacadeTests(unittest.TestCase):
    def test_facade_exports_implementation_types_and_routes(self):
        self.assertIs(MovieCommands, ImplementationCommands)
        self.assertIs(event_router, implementation_router)

    def test_state_remains_backend_owned_and_survives_new_instances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media.sqlite3"
            store = EventStore(path)
            self.assertFalse(path.exists())
            store.remember_movie("cube", Movie(1, "Dune", "2021"))
            self.assertEqual(EventStore(path).remembered_movie("cube").title, "Dune")

    def test_public_close_retains_client_cleanup(self):
        commands = MovieCommands(client=Mock())
        close_commands(commands)
        commands.client.close.assert_called_once_with()
