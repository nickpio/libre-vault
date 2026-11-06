"""Trusted, app-local Python plugins and their window-scoped API."""
from dataclasses import dataclass, field
import logging
from pathlib import Path
import sys
from types import ModuleType
from uuid import uuid4

from .editing import TextEdit


API_VERSION = 1
logger = logging.getLogger(__name__)


@dataclass
class Plugin:
    identifier: str
    module: ModuleType
    commands: dict = field(default_factory=dict)
    cleanups: list = field(default_factory=list)
    active: bool = True
    registering: bool = True


class PluginAPI:
    """Convenience API, not a sandbox. Resolve the active note on every call."""

    version = API_VERSION

    def __init__(self, window, plugin):
        self._window = window
        self._plugin = plugin

    def _check_active(self):
        if not self._plugin.active:
            raise RuntimeError('This plugin has been unloaded.')

    def register_command(self, identifier, title, callback):
        self._check_active()
        if not self._plugin.registering:
            raise RuntimeError('Register commands during setup().')
        if not isinstance(identifier, str) or not identifier.isidentifier():
            raise ValueError('Command IDs must be Python identifiers.')
        if not isinstance(title, str) or not title.strip():
            raise ValueError('Command titles must not be empty.')
        if not callable(callback):
            raise TypeError('Command callbacks must be callable.')
        if identifier in self._plugin.commands:
            raise ValueError(f'Duplicate command ID: {identifier}')
        self._plugin.commands[identifier] = (title.strip(), callback)

    def on_unload(self, callback):
        self._check_active()
        if not callable(callback):
            raise TypeError('Unload callbacks must be callable.')
        self._plugin.cleanups.append(callback)

    @property
    def vault_root(self):
        self._check_active()
        vault = self._window.vault
        return vault.root if vault is not None else None

    @property
    def current_note_path(self):
        self._check_active()
        pane = self._window.current()
        return pane.note.path if pane is not None else None

    def _pane(self):
        self._check_active()
        pane = self._window.current()
        if pane is None:
            raise RuntimeError('Open a Markdown note first.')
        return pane

    def get_text(self):
        return self._pane().text()

    def get_selection(self):
        pane = self._pane()
        bounds = pane.buffer.get_selection_bounds()
        if not bounds:
            return ''
        start, end = (iterator.get_offset() for iterator in bounds)
        return pane.text()[start:end]

    def replace_selection(self, text):
        if not isinstance(text, str):
            raise TypeError('Replacement text must be a string.')
        pane = self._pane()
        bounds = pane.buffer.get_selection_bounds()
        if bounds:
            start, end = (iterator.get_offset() for iterator in bounds)
        else:
            start = end = pane.buffer.get_iter_at_mark(pane.buffer.get_insert()).get_offset()
        pane.apply_edit(TextEdit(start, end, text))

    def show_status(self, message):
        self._check_active()
        self._window.status.set_text(str(message))


class PluginManager:
    def __init__(self, window, directory):
        self.window = window
        self.directory = Path(directory)
        self.enabled = False
        self.plugins = {}
        self.errors = []

    def _error(self, identifier, phase, error):
        message = f'{identifier} ({phase}): {type(error).__name__}: {error}'
        self.errors.append(message)
        self.errors = self.errors[-20:]
        logger.error('Plugin %s', message, exc_info=True)
        if not self.window.closed:
            self.window.status.set_text(f'Plugin error · {message}')

    def set_enabled(self, enabled):
        if enabled == self.enabled:
            return
        self.enabled = enabled
        if enabled:
            self.reload()
        else:
            self.unload()

    def reload(self):
        self.errors.clear()
        self.unload()
        if not self.enabled:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            paths = sorted(self.directory.iterdir())
        except OSError as error:
            self._error('plugins', 'discovery', error)
            return
        for path in paths:
            if (path.suffix == '.py' and path.stem.isidentifier()
                    and not path.stem.startswith('_') and not path.is_symlink() and path.is_file()):
                self._load(path)

    def _load(self, path):
        name = f'_librevault_plugin_{uuid4().hex}_{path.stem}'
        module = ModuleType(name)
        module.__file__ = str(path)
        plugin = Plugin(path.stem, module)
        sys.modules[name] = module
        try:
            exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
            if type(getattr(module, 'API_VERSION', None)) is not int or module.API_VERSION != API_VERSION:
                raise ValueError(f'Plugin must declare API_VERSION = {API_VERSION}.')
            setup = getattr(module, 'setup', None)
            if not callable(setup):
                raise ValueError('Plugin must define setup(api).')
            setup(PluginAPI(self.window, plugin))
        except (Exception, SystemExit) as error:
            self._error(path.stem, 'load', error)
            self._unload(plugin)
        else:
            plugin.registering = False
            self.plugins[plugin.identifier] = plugin

    def command_actions(self):
        actions = []
        for plugin in self.plugins.values():
            for identifier, (title, _) in plugin.commands.items():
                actions.append((f'{plugin.identifier}: {title}',
                                lambda plugin=plugin, identifier=identifier: self.run(plugin, identifier)))
        return actions

    def run(self, plugin, identifier):
        if not self.enabled or self.plugins.get(plugin.identifier) is not plugin:
            return
        try:
            plugin.commands[identifier][1]()
        except (Exception, SystemExit) as error:
            self._error(plugin.identifier, f'command {identifier}', error)

    def _unload(self, plugin):
        plugin.active = False
        plugin.registering = False
        for callback in reversed(plugin.cleanups):
            try:
                callback()
            except (Exception, SystemExit) as error:
                self._error(plugin.identifier, 'unload', error)
        plugin.cleanups.clear()
        plugin.commands.clear()
        sys.modules.pop(plugin.module.__name__, None)

    def unload(self):
        plugins = list(self.plugins.values())
        self.plugins.clear()
        for plugin in reversed(plugins):
            self._unload(plugin)
