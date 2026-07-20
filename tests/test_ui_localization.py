"""Regression checks for visible controls that previously bypassed i18n."""

from pathlib import Path


HTML = (Path(__file__).parents[1] / "app" / "web" / "index.html").read_text(
    encoding="utf-8")


def test_translation_targets_have_truthful_labels_and_working_swap_button():
    assert 'data-i18n="hear"' in HTML
    assert 'data-i18n="to_other"' in HTML
    assert 'id="langswap"' in HTML
    assert 'data-i18n-title="swap_languages"' in HTML
    assert 'ru:{hear:"Я слышу",to_other:"Собеседник слышит"' in HTML


def test_idle_meter_does_not_claim_that_audio_capture_is_running():
    assert 'id="vad" role="status" aria-live="polite"' in HTML
    assert 'data-i18n-aria="waiting_signal"' in HTML
    assert 'ru:"Запустите перевод для проверки сигнала"' in HTML


def test_live_meter_separates_raw_system_audio_from_speech_detection():
    assert '.meter.signal .rods i{background:var(--green)}' in HTML
    assert "T(hasInputSignal ? 'system_audio_detected' : 'waiting_system_audio')" in HTML
    assert 'system_audio_detected:"Системный звук есть · речи пока нет"' in HTML
    assert "$('#mic').disabled = (p.mode==='video')" in HTML
    assert 'mic_meeting_only:"Микрофон · только для встречи"' in HTML


def test_history_list_accessible_name_follows_interface_language():
    assert 'id="history-list" role="listbox"' in HTML
    assert 'data-i18n-aria="history_title"' in HTML


def test_outgoing_translation_monitor_is_explicit_and_localized():
    assert 'id="monitor-outgoing"' in HTML
    assert 'data-i18n="monitor_outgoing"' in HTML
    assert 'data-i18n-title="monitor_outgoing_hint"' in HTML
    assert 'monitor_outgoing:"Слушать мой перевод"' in HTML
