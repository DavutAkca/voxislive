"""transcript_store: bilingual vs translated-only export rendering."""
import app.transcript_store as ts


def _rec():
    return {
        "version": 1,
        "started": 0.0,
        "turns": [
            {"t": 0.0, "dir": "in", "src": "Merhaba dünya", "text": "Hello world"},
            {"t": 3.0, "dir": "in", "src": "Nasilsin", "text": "How are you"},
            # A turn with no captured source: bilingual output falls back to the
            # translation line alone (no stray blank source line).
            {"t": 6.0, "dir": "in", "src": "", "text": "Fine thanks"},
        ],
    }


def test_txt_mono_is_translation_only():
    out = ts.render_txt(_rec())
    assert out == "Hello world\nHow are you\nFine thanks\n"
    assert "Merhaba" not in out


def test_txt_bilingual_pairs_source_over_translation():
    out = ts.render_txt(_rec(), bilingual=True)
    assert "Merhaba dünya\nHello world" in out
    assert "Nasilsin\nHow are you" in out
    # Blank line between turns; source-less turn keeps only the translation.
    assert out.endswith("Fine thanks\n")
    assert "\n\nFine thanks" in out


def test_srt_bilingual_default_and_mono_opt_out():
    bi = ts.render_srt(_rec())
    assert "Merhaba dünya" in bi and "Hello world" in bi
    mono = ts.render_srt(_rec(), bilingual=False)
    assert "Merhaba dünya" not in mono and "Hello world" in mono


def test_export_passes_bilingual_flag():
    content, ext = ts.export(_rec(), "txt", bilingual=True)
    assert ext == "txt" and "Merhaba dünya" in content
    content, ext = ts.export(_rec(), "txt", bilingual=False)
    assert "Merhaba dünya" not in content


def test_export_unknown_format_raises():
    import pytest
    with pytest.raises(ValueError):
        ts.export(_rec(), "pdf")
