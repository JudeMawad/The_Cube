"""Public media API; importing it creates no clients or persistent state."""
from .movie_commands import MovieCommands
from .media_events import EventStore
from .event_routes import event_router


def close_commands(commands):
    commands.client.close()


from .tools import tool_definitions

__all__ = ["MovieCommands", "EventStore", "event_router", "close_commands", "tool_definitions"]
