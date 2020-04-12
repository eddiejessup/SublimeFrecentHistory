# pylint: disable=import-error
# pylint: disable=no-else-raise
# pylint: disable=no-else-return
# pylint: disable=no-else-continue

from collections import defaultdict
from contextlib import contextmanager
from enum import Enum
import functools
import itertools
import json
import os.path
import pathlib
import time

import sublime
import sublime_plugin

from . import natural  # pylint: disable=relative-beyond-top-level

HOME = os.path.expanduser('~')

SAVE_EVERY = 50

# Utilities.

def get_time_seconds():
    return int(time.time())

# Get a generator that returns `True` every `n` calls, `False` otherwise.
# Useful to avoid noise of `count +=1; if count % n == 0: (foo; count = 0)`
def true_every(n):
    gen = (i == 0 for i in itertools.cycle(range(n)))
    # Return `False` on first call.
    next(gen)
    return gen

# /Utilities.

# Settings.

SETTINGS_FILE_NAME = 'FrecentHistory.sublime-settings'

# I want to provide reasonable defaults, but the package also should provide
# default settings, and I don't want to state defaults in two places. So opt to
# throw an error if we can't find the defaults.
def get_setting(key):
    value = sublime.load_settings(SETTINGS_FILE_NAME).get(key)
    if value is None:
        raise KeyError(key)
    else:
        return value

def get_print_debug():
    return get_setting('print_debug')

def get_show_file_preview():
    return get_setting('show_file_preview')

def get_max_master_entries():
    return get_setting('max_master_entries')

def get_history_path():
    return os.path.expanduser(get_setting('history_path'))

# /Settings.

# Logging.

def log_debug(text):
    if get_print_debug():
        print('[FrecentHistory] ' + text)

# Helper to time execution of chunks of code.
@contextmanager
def timed_operation(label):
    pre = time.time()
    try:
        yield
    finally:
        log_debug(f'"{label}" took {1000 * (time.time() - pre):.0g} milliseconds')

# /Logging.

# Global state.

def new_history_entry():
    now = get_time_seconds()
    return dict(added=now, last_seen=now, inserts=0)

# Global state to coordinate the event-listener tracking views, and the window
# commands. Maybe you could write this to avoid mutable global state, but it
# might turn out to be more difficult than you expect.
global_state = {
    # Global dict mapping path to path attributes.
    # In general this structure is a 'history'.
    'master_history': defaultdict(new_history_entry),

    # Map from window-ID to 'history' relevant to that window. In practice the
    # values are shared objects with the master list, and maybe across
    # windows.
    'window_histories': defaultdict(dict),

    # Whether the window quick-panel is open. Don't mutate the state while it's
    # open, or you might crash Sublime.
    'active': False,

    # If we notice a path no longer exists, we can put it here to remove from
    # the history later, to avoid slowing down operations by doing it at the
    # time.
    'paths_to_remove': set(),

    # We want to save every `n` operations. We use this generator to track how
    # many operations we've done.
    'save_cycle': true_every(SAVE_EVERY),
}

# Just a wee helper for a common operation, no grand principles at play.
def get_window_history(window):
    return global_state['window_histories'][window.id()]

def record_seen_path_in_window(window, path, now):
    window_history = get_window_history(window)

    # Add/update entry in master history.
    entry = global_state['master_history'][path]
    log_debug(f'Adding/Updating {path}')
    entry['last_seen'] = now
    entry['inserts'] += 1

    # Add entry to window history if necessary.
    window_history[path] = entry

    if next(global_state['save_cycle']):
        log_debug('Saving...')
        save_master_history_to_file(get_history_path(), now=now)

# Merge one source of history into another. Our implementation is optimised for
# the case where the history-to-merge is smaller than the
# history-to-be-updated.
def merge_histories(mergee_history, merger_history):
    for path, merger_entry in merger_history.items():
        if path in mergee_history:
            mergee_entry = mergee_history[path]
            mergee_entry['last_seen'] = max(mergee_entry['last_seen'], merger_entry['last_seen'])
            mergee_entry['inserts'] = max(mergee_entry['inserts'], merger_entry['inserts'])
            mergee_entry['added'] = min(mergee_entry['added'], merger_entry['added'])
        else:
            mergee_history[path] = merger_entry

def load_and_populate_state_from_file(store_path):
    # First we load the master list from the stored file.
    with timed_operation('Load master history'):
        load_master_history_from_file(store_path)
    # Then we populate the window histories.
    with timed_operation('Populate window histories'):
        for window in sublime.windows():
            # There are two sources of data to populate:
            # - The master list, which might have entries for files relevant to
            #   our window.
            # - The already-open files in the window.
            populate_window_history_from_master(window)
            populate_window_history_from_views(window)

def load_master_history_from_file(store_path):
    log_debug(f'Loading from {store_path}')
    try:
        with timed_operation('Fetch saved history'):
            with open(store_path, 'r') as f:
                stored_master_history = json.load(f)
    except IOError as e:
        log_debug(f'Could not load store at {store_path}: {e}')
    else:
        log_debug(f'Found {len(stored_master_history)} stored entries')
        # Incorporate any history we might have accumulated before the load.
        with timed_operation('Set saved history'):
            merge_histories(
                mergee_history=stored_master_history,
                merger_history=global_state['master_history'],
            )
            global_state['master_history'].update(stored_master_history)

def save_master_history_to_file(store_path, now):
    # Avoid saving entries that will be deleted anyway.
    remove_paths_to_remove()
    with open(store_path, 'a+') as f:
        f.seek(0)
        try:
            stored_master_history = json.load(f)
        except json.decoder.JSONDecodeError:
            stored_master_history = {}
        state_master_history = limit_entries(
            global_state['master_history'],
            n=get_max_master_entries(),
            now=now,
        )
        # I don't want to lose information. We might have a couple fewer
        # entries because of file deletions, but if we are about to write many
        # fewer entries, that might be a sign we are about to do something we
        # regret, so let's just not do anything.
        if len(state_master_history) > 0.7 * len(stored_master_history):
            f.truncate(0)
            log_debug(f'Saving {len(state_master_history)} entries to {store_path}')
            json.dump(state_master_history, f, allow_nan=False, sort_keys=True, indent=2)

def populate_window_history_from_master(window):
    window_history = get_window_history(window)

    window_folders = window.folders()
    master_history = global_state['master_history']
    for folder in window_folders:
        for path in master_history:
            if path.startswith(folder):
                window_history[path] = master_history[path]
        log_debug(
            f'Populated window "{window.id()}" history with master entries under {folder}'
        )

def populate_window_history_from_views(window):
    now = get_time_seconds()
    for view in window.views():
        record_view_in_window(view, now)

def limit_entries(entries, n, now):
    return dict(sorted(
        entries.items(),
        # Sort the list with the highest score first.
        reverse=True,
        # The score for an entry is its 'frecency', which combines how many
        # times we have seen this entry, and how recently we last saw it.
        key=lambda x: entry_frecency(x[1], now)
    )[:n])  # Take the first 'n' entries with the highest score.

def entry_frecency(entry, now):
    return frecency(
        age=max(100, now - entry['last_seen']),
        count=entry['inserts'],
    )

def historied_path_exists(path):
    if os.path.exists(path):
        return True
    else:
        log_debug(f'Could not find path {path}, adding to garbage')
        global_state['paths_to_remove'].add(path)
        return False

def remove_paths_to_remove():
    for path_to_remove in global_state['paths_to_remove']:
        log_debug(f'Removing garbage path {path_to_remove}')
        for window_history in global_state['window_histories'].values():
            window_history.pop(path_to_remove, None)
        global_state['master_history'].pop(path_to_remove, None)

def record_view_in_window(view, now):
    window = view.window()
    path = view.file_name()
    # Only track views with a path, and not transient views.
    if (path is not None and window is not None and not global_state['active']
            and os.path.exists(path)):
        record_seen_path_in_window(window, path, now)

# /Global state.

# Frecency.

# This heuristic is ripped off of the command-line tool 'fasd', who I think
# ripped it off of Mozilla. So credit goes to some combination of them. It
# tries to combine the frequency of access, and the recency of access, as both
# should increase our confidence the file someone is looking for is that given
# file.

SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = SECONDS_PER_MINUTE * 60
SECONDS_PER_DAY = SECONDS_PER_HOUR * 24
SECONDS_PER_WEEK = SECONDS_PER_DAY * 7

def frecency(age, count):
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

# /Frecency.

# Event listener.

# We record the view when a file is 'activated', basically viewed, opened and
# so on.
class OpenFrecentFileEvent(sublime_plugin.EventListener):  # pylint: disable=too-few-public-methods

    def on_activated_async(self, view):  # pylint: disable=no-self-use
        record_view_in_window(view, now=get_time_seconds())

# /Event listener.

# Comand.

# Abbreviate some prefixes to render paths more nicely.
def shorten_path(path, heres):
    path_path = pathlib.Path(path)
    abbrevs = [(here, '.') for here in heres] + [(HOME, '~')]
    for prefix, abbrev in abbrevs:
        try:
            return str(abbrev / path_path.relative_to(prefix))
        except ValueError:
            continue

def render_access_count(x):
    if x == 0:
        return 'not seen'
    elif x == 1:
        return 'seen once'
    elif x == 2:
        return 'seen twice'
    else:
        return 'seen {} times'.format(natural.number.word(x, digits=1))

def render_subtitle(attrs):
    return '{}, {}, {:.2g}%'.format(
        # '-1' is to avoid a 'zero' last-seen, which would get rendered as '0
        # seconds from now' like it's in the future.
        natural.date.duration(attrs["last_seen"] - 1, precision=2),
        render_access_count(attrs["inserts"]),
        100 * attrs["score_frac"],
    )

# Classify files with a little symbol.
# Circle/Diamond = yes/no from around here (is path within one of the window folders)
# Filled/Empty = opened / not-open
def get_symbol(is_open, is_within_folders):
    if is_open:
        if is_within_folders:
            return '•'
        else:
            return '◆'
    else:
        if is_within_folders:
            return ' '
        else:
            return '◇'

class OpenStatusFilter(Enum):
    OPENED = 'opened'
    CLOSED = 'closed'
    BOTH = 'both'

def get_data_list_for_panel(history, window, open_status_filter):
    now = get_time_seconds()
    window_folders = window.folders()

    for path, attrs in history.items():
        is_open = window.find_open_file(path) is not None

        if ((is_open and open_status_filter == OpenStatusFilter.CLOSED)
                or (not is_open and open_status_filter == OpenStatusFilter.OPENED)):
            continue
        else:
            yield dict(
                path=path,
                score=entry_frecency(attrs, now),
                is_open=is_open,
                is_within_folders=any(path.startswith(folder) for folder in window_folders),
                **attrs
            )

class OpenFrecentFileCommand(sublime_plugin.WindowCommand):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with timed_operation('Load state from file'):
            load_and_populate_state_from_file(get_history_path())

    def run(self, use_master=False, open_status_filter=OpenStatusFilter.BOTH.value):
        try:
            open_status_filter = OpenStatusFilter(open_status_filter)
        except ValueError:
            log_debug(f'Got unexpected open_status_filter: {open_status_filter}')

        history = (
            global_state['master_history']
            if use_master
            else get_window_history(self.window)
        )

        with timed_operation('Get panel data'):
            entry_data_list = sorted(
                get_data_list_for_panel(history, self.window, open_status_filter),
                key=lambda x: x['score'],
                reverse=True,
            )
            # TODO: We could accumulate the score above to avoid iterating
            # twice. But it seems like the rendering below dominates our
            # runtime so let's not bother.
            total_score = sum(attrs['score'] for attrs in entry_data_list)
            for attrs in entry_data_list:
                attrs['score_frac'] = attrs['score'] / total_score

        with timed_operation('Render display list'):
            entry_display_list = [
                [
                    '{} {}'.format(
                        get_symbol(attrs['is_open'], attrs['is_within_folders']),
                        shorten_path(attrs['path'], self.window.folders()),
                    ),
                    render_subtitle(attrs),
                ]
                for attrs in entry_data_list
            ]

        global_state['active'] = True
        self.window.show_quick_panel(
            entry_display_list,
            functools.partial(self.open_file, entry_data_list, self.window.active_view()),
            flags=sublime.KEEP_OPEN_ON_FOCUS_LOST,
            on_highlight=functools.partial(self.preview_selection, entry_data_list),
            selected_index=0,
        )

    def preview_selection(self, entry_data_list, selected_index):
        if selected_index >= 0 and get_show_file_preview():
            path = entry_data_list[selected_index]['path']
            if historied_path_exists(path):
                self.window.open_file(
                    path,
                    sublime.FORCE_GROUP | sublime.TRANSIENT
                )

    def open_file(self, entry_data_list, original_view, selected_index):
        global_state['active'] = False

        # Cancelled entry, focus on active view when comand was run.
        if selected_index < 0:
            self.window.focus_view(original_view)
        else:
            path = entry_data_list[selected_index]['path']
            if historied_path_exists(path):
                self.window.open_file(
                    path,
                )

        # We might have found some paths that didn't exist during our
        # previewing, so collect any garbage.
        remove_paths_to_remove()

# /Comand.
