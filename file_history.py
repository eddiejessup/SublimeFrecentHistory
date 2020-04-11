# pylint: disable=import-error
# pylint: disable=no-else-return

import functools
import os.path
import time
from collections import defaultdict



import sublime
import sublime_plugin

from . import natural  # pylint: disable=relative-beyond-top-level

SETTINGS_FILE = 'FileHistory.sublime-settings'
DEFAULT_TIMESTAMP_FORMAT = '%Y-%m-%d @ %H:%M:%S'

def get_settings():
    return sublime.load_settings(SETTINGS_FILE)

def get_show_file_preview():
    return get_settings().get('show_file_preview', True)

def get_print_debug():
    return get_settings().get('debug', False)

def get_global_max_entries():
    return get_settings().get('global_max_entries', 100)

def get_history_path():
    package_rel_path = get_settings().get(
        'history_file',
        os.path.join('User', 'FileHistory.json')
    )
    return os.path.normpath(os.path.join(sublime.packages_path(), package_rel_path))

def get_use_monospace():
    return get_settings().get('monospace_font', False)

def get_real_path():
    return get_settings().get('real_path', False)

def get_path_exclude_patterns():
    return get_settings().get('path_exclude_patterns', [])

def log_debug(text):
    if get_print_debug():
        print('[FileHistory] ' + text)

def new_window_history():
    return defaultdict(new_history_entry)

def new_history_entry():
    now = int(time.time())
    return dict(added=now, last_seen=now, inserts=0)

global_state = {
    'window_id_to_history': defaultdict(new_window_history),
    'active': False,
    'absent_paths': set(),
    'inserts_since_save': 0
}

# History interface.

def set_window_history(window, window_history):
    global_state['window_id_to_history'][window.id()] = window_history

def get_window_history(window):
    return global_state['window_id_to_history'][window.id()]

def add_to_window_history(window, path):
    window_history = get_window_history(window)
    now = int(time.time())

    entry = window_history[path]
    log_debug(f'Adding/Updating {path}')
    entry['last_seen'] = now
    entry['inserts'] += 1

    global_state['inserts_since_save'] += 1

    if global_state['inserts_since_save'] % 10 == 0:
        log_debug('Cleaning up...')
        clean_absent_paths(window)


def prune_window_history(window):
    now = int(time.time())
    # Update the window history.
    set_window_history(
        window,
        # Sort the window history, by decreasing 'score', where a higher score
        # indicates we should keep the entry.
        dict(sorted(
            # Start from the raw window history.
            get_window_history(window).items(),
            # Sort the list with the highest score first.
            reverse=True,
            # The score for an entry is its 'frecency', which combines how many
            # times we have seen this entry, and how recently we last saw it.
            key=lambda x: entry_frecency(
                age=max(100, now - x[1]['last_seen']),
                count=x[1]['inserts'],
            )
        )[:get_global_max_entries()])  # Take the first 'n' entries with the highest score.
    )

def historied_path_exists(path):
    if os.path.exists(path):
        return True
    else:
        global_state['absent_paths'].add(path)
        return False

def clean_absent_paths(window):
    window_history = get_window_history(window)
    for absent_path in global_state['absent_paths']:
        window_history.pop(absent_path, None)

# /History interface.

def add_view_to_history(view):
    window = view.window()
    path = view.file_name()
    # Only track views with a path, and not transient views.
    if path is not None and window is not None and not global_state['active']:
        add_to_window_history(window, path)

class OpenRecentlyClosedFileEvent(sublime_plugin.EventListener):  # pylint: disable=too-few-public-methods

    def on_activated_async(self, view):  # pylint: disable=no-self-use
        add_view_to_history(view)

def shorten_path(path, prefixes):
    for prefix in prefixes:
        r = os.path.relpath(path, prefix)
        if not r.startswith('..'):
            return r
    home = os.path.expanduser("~/")
    if path.startswith(home):
        path = '~/' + path[len(home):]
    return path

def render_duration(x):
    return natural.date.duration(x, precision=2)

def render_number(x):
    if x == 0:
        return 'not seen'
    elif x == 1:
        return 'seen once'
    elif x == 2:
        return 'seen twice'
    else:
        return 'seen {} time{}'.format(
            natural.number.word(x, digits=1),
            '' if x == 1 else 's'
        )

class OpenRecentlyClosedFileCommand(sublime_plugin.WindowCommand):

    def run(self):
        # Prepare the display list with the file name and path separated
        data_list = [
            dict(path=path, **attrs)
            for path, attrs in get_window_history(self.window).items()
        ]
        display_list = [
            [
                shorten_path(attrs['path'], self.window.folders()),
                f'{render_duration(attrs["last_seen"])}, {render_number(attrs["inserts"])}'
            ]
            for attrs in data_list
        ]

        global_state['active'] = True
        self.window.show_quick_panel(
            display_list,
            functools.partial(self.open_file, data_list, self.window.active_view()),
            flags=sublime.KEEP_OPEN_ON_FOCUS_LOST,
            on_highlight=functools.partial(self.preview_selection, data_list),
            selected_index=0,
        )

    def preview_selection(self, data_list, selected_index):
        if get_show_file_preview() and selected_index >= 0:
            selected_entry = data_list[selected_index]
            path = selected_entry['path']
            if historied_path_exists(path):
                self.window.open_file(path, sublime.TRANSIENT | sublime.FORCE_GROUP)

    def open_file(self, data_list, original_view, selected_index):
        global_state['active'] = False

        # Cancelled entry, focus on active view when comand was run.
        if selected_index < 0:
            self.window.focus_view(original_view)
        else:
            selected_entry = data_list[selected_index]
            path = selected_entry['path']
            if historied_path_exists(path):
                self.window.open_file(path, sublime.FORCE_GROUP)
            else:
                log_debug(f'Path does not exist: {path}')
                global_state['absent_paths'].add(path)

        clean_absent_paths(self.window)

# Frecency.

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = SECONDS_PER_MINUTE * 60
SECONDS_PER_DAY = SECONDS_PER_HOUR * 24
SECONDS_PER_WEEK = SECONDS_PER_DAY * 7

def entry_frecency(age, count):
    return (count / age) * recency_score(age)

def recency_score(ds):
    if ds < SECONDS_PER_MINUTE:
        return 8
    if ds < SECONDS_PER_HOUR:
        return 6
    if ds < SECONDS_PER_DAY:
        return 4
    if ds < SECONDS_PER_WEEK:
        return 2
    else:
        return 1
