"""Windows instance-guard behavior and its user-controlled opt-in."""
import sys
import types

import main


class _Kernel32:
    def __init__(self, handle=101):
        self.handle = handle
        self.closed = []

    def CreateMutexW(self, _attrs, _owner, _name):
        return self.handle

    def CloseHandle(self, handle):
        self.closed.append(handle)


class _User32:
    def __init__(self, hwnd=202):
        self.hwnd = hwnd
        self.shown = []
        self.focused = []

    def FindWindowW(self, _class_name, _title):
        return self.hwnd

    def ShowWindow(self, hwnd, command):
        self.shown.append((hwnd, command))

    def SetForegroundWindow(self, hwnd):
        self.focused.append(hwnd)


def _fake_ctypes(monkeypatch, *, already=True, hwnd=202):
    kernel = _Kernel32()
    user = _User32(hwnd)
    module = types.ModuleType("ctypes")
    module.WinDLL = lambda _name, use_last_error=True: kernel
    module.get_last_error = lambda: 183 if already else 0
    module.windll = types.SimpleNamespace(user32=user)
    monkeypatch.setitem(sys.modules, "ctypes", module)
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main, "_INSTANCE_MUTEX", None)
    return kernel, user


def test_second_instance_is_blocked_by_default(monkeypatch):
    kernel, user = _fake_ctypes(monkeypatch, already=True)

    assert main._acquire_single_instance() is False

    assert kernel.closed == [kernel.handle]
    assert user.shown == [(user.hwnd, 9)]
    assert user.focused == [user.hwnd]
    assert main._INSTANCE_MUTEX is None


def test_user_can_allow_an_additional_instance(monkeypatch):
    kernel, user = _fake_ctypes(monkeypatch, already=True)

    assert main._acquire_single_instance(allow_multiple=True) is True

    # Keep the shared named-mutex handle alive. If the preference is later
    # disabled, another launch will still detect every running Voxis process.
    assert main._INSTANCE_MUTEX == kernel.handle
    assert kernel.closed == []
    assert user.shown == []


def test_first_instance_keeps_mutex_with_either_setting(monkeypatch):
    kernel, _user = _fake_ctypes(monkeypatch, already=False)

    assert main._acquire_single_instance(allow_multiple=False) is True
    assert main._INSTANCE_MUTEX == kernel.handle
    assert kernel.closed == []


def test_main_passes_saved_preference_to_instance_guard(monkeypatch):
    seen = []
    monkeypatch.setattr(main, "load_dotenv", lambda: None)
    monkeypatch.setattr(main, "_setup_logging", lambda: None)
    monkeypatch.setattr(
        main, "load_config",
        lambda: {"ui_language": "ru", "allow_multiple_instances": True},
    )
    monkeypatch.setattr(main.i18n, "set_language", lambda _lang: None)
    monkeypatch.setattr(
        main, "_acquire_single_instance",
        lambda allow_multiple=False: seen.append(allow_multiple) or True,
    )
    # Stop immediately after the guard; later startup behavior is outside this
    # unit and imports platform-specific audio components.
    monkeypatch.setattr(main, "_preflight_webview2", lambda: False)

    main.main()

    assert seen == [True]
