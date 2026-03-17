from sand_app.actions.local_sand import local_sand_action_entry_point
from sand_app.actions.remote_sand import remote_sand_action_entry_point
from sand_app.actions.simple_action import simple_action_entry_point

__all__ = [
    'simple_action_entry_point',
    'remote_sand_action_entry_point',
    'local_sand_action_entry_point',
]
