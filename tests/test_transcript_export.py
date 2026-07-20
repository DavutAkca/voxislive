"""transcript_store: bilingual vs translated-only export rendering."""
from concurrent.futures import ThreadPoolExecutor

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


def test_source_only_turn_survives_record_and_bilingual_exports():
    rec = ts.build_record(1.0, [
        {"t": 0, "dir": "out", "src": "Source survived", "text": ""},
    ])
    assert rec["turns"] == [
        {"t": 0.0, "dir": "out", "src": "Source survived", "text": ""},
    ]
    assert ts.render_txt(rec, bilingual=True) == "Source survived\n"
    assert "Source survived" in ts.render_srt(rec, bilingual=True)
    assert ts.render_txt(rec, bilingual=False) == ""


def test_concurrent_saves_always_leave_valid_json(tmp_path):
    records = [ts.build_record(1.0, [
        {"t": 0, "src": f"source-{i}", "text": f"translation-{i}"},
    ]) for i in range(24)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        paths = list(pool.map(
            lambda rec: ts.save_record(str(tmp_path), rec, subdir="voxis_same"),
            records))
    assert len(set(paths)) == 1
    saved = ts.load_record(paths[0])
    assert saved in records
    assert not list((tmp_path / "voxis_same").glob("*.tmp"))


def test_failed_save_preserves_the_previous_good_record(tmp_path, monkeypatch):
    good = ts.build_record(1.0, [{"t": 0, "src": "one", "text": "good"}])
    path = ts.save_record(str(tmp_path), good, subdir="voxis_atomic")

    def broken_dump(record, f, **kwargs):
        f.write('{"partial":')
        raise OSError("disk interrupted")

    monkeypatch.setattr(ts.json, "dump", broken_dump)
    import pytest
    with pytest.raises(OSError, match="disk interrupted"):
        ts.save_record(
            str(tmp_path), {"version": 1, "turns": []}, subdir="voxis_atomic")
    assert ts.load_record(path) == good
    assert not list((tmp_path / "voxis_atomic").glob("*.tmp"))
