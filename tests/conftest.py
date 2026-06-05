"""Shared pytest fixtures and lightweight test doubles.

The plugin's two pure-logic modules (``protocol`` and ``controller``)
own background threads and a WebSocket transport, but every code path we
care about for unit testing is reachable without touching the network.
``FakeAdapter`` stands in for :class:`PandaProtocolAdapter` so the
controller can be exercised in isolation, recording the high-level
commands it would have sent to the device.
"""

import sys
import types
from typing import Optional

import pytest


def _install_octoprint_stubs():
    """Stub the OctoPrint / Flask stack so the plugin package imports.

    ``octoprint_pandabreath/__init__.py`` is the OctoPrint plugin glue and
    pulls in flask + the full OctoPrint runtime at import time. That layer
    needs a live OctoPrint harness to test meaningfully and is excluded
    from the coverage target; the pure-logic submodules (protocol,
    controller, frame_log) are what we measure. To import those submodules
    Python first executes the package ``__init__``, so we register minimal
    stand-ins for the heavy third-party modules it imports at top level.
    Real installs are unaffected — these stubs only ever load under pytest,
    and only when the real packages are absent.
    """

    def _module(name):
        mod = sys.modules.get(name)
        if mod is None:
            mod = types.ModuleType(name)
            sys.modules[name] = mod
        return mod

    # flask + flask_babel.gettext
    try:
        import flask  # noqa: F401  # pylint: disable=unused-import
    except ImportError:
        _module("flask")
        babel = _module("flask_babel")
        # setattr (not ``babel.gettext = ...``) so static checkers don't
        # flag attribute assignment on the dynamically-built ModuleType.
        setattr(babel, "gettext", lambda s, *a, **k: s)

    # octoprint.plugin mixins, access control, events, util.RepeatedTimer
    try:
        import octoprint  # noqa: F401  # pylint: disable=unused-import
    except ImportError:
        octoprint = _module("octoprint")
        plugin = _module("octoprint.plugin")
        # The plugin class multiply-inherits from these mixins; empty
        # classes are enough for the module to import.
        for mixin in (
            "StartupPlugin",
            "ShutdownPlugin",
            "SettingsPlugin",
            "AssetPlugin",
            "TemplatePlugin",
            "SimpleApiPlugin",
            "EventHandlerPlugin",
            "WizardPlugin",
            "RestartNeedingPlugin",
        ):
            setattr(plugin, mixin, type(mixin, (object,), {}))

        # BlueprintPlugin.route is used as a decorator at class-body time
        # (@BlueprintPlugin.route(...)); make it a no-op decorator factory.
        def _route(*_a, **_k):
            return lambda fn: fn

        setattr(
            plugin,
            "BlueprintPlugin",
            type(
                "BlueprintPlugin",
                (object,),
                {"route": staticmethod(_route)},
            ),
        )
        setattr(octoprint, "plugin", plugin)

        access = _module("octoprint.access")
        setattr(access, "ADMIN_GROUP", "admins")
        perms = _module("octoprint.access.permissions")
        setattr(perms, "Permissions", type("Permissions", (object,), {}))
        setattr(access, "permissions", perms)

        events = _module("octoprint.events")
        setattr(events, "Events", type("Events", (object,), {}))

        util = _module("octoprint.util")
        setattr(util, "RepeatedTimer", type("RepeatedTimer", (object,), {}))
        setattr(octoprint, "util", util)
        setattr(octoprint, "access", access)
        setattr(octoprint, "events", events)


_install_octoprint_stubs()


class FakeAdapter:
    """Minimal stand-in for ``PandaProtocolAdapter``.

    Records ``send_command`` calls as ``(command, params)`` tuples and
    lets each test dictate connection / observe-only / last-rx state.
    """

    def __init__(self, connected=True, observe_only=False, last_rx=0.0):
        self.commands = []
        self._connected = connected
        self._observe_only = observe_only
        self._last_rx = last_rx
        self.force_reconnect_calls = 0
        # When set, ``send_command`` raises this to exercise error paths.
        self.raise_on_send: Optional[Exception] = None

    def send_command(self, command, **params):
        """Record a command; optionally raise to exercise error paths."""
        self.commands.append((command, params))
        if self.raise_on_send is not None:
            raise self.raise_on_send
        return True

    def is_connected(self):
        """Return the configured connection state."""
        return self._connected

    def is_observe_only(self):
        """Return the configured observe-only state."""
        return self._observe_only

    def last_rx_timestamp(self):
        """Return the configured last-rx timestamp."""
        return self._last_rx

    def force_reconnect(self):
        """Count a reconnect request."""
        self.force_reconnect_calls += 1

    # ---- test helpers ----

    def last_command(self):
        """Return the most recent recorded command, or None."""
        return self.commands[-1] if self.commands else None

    def command_names(self):
        """Return the recorded command verbs in order."""
        return [c for c, _ in self.commands]


@pytest.fixture
def adapter():
    """A connected, writable fake adapter."""
    return FakeAdapter()
