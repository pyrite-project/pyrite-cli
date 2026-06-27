from pathlib import Path

from cli.utils.traceback_map import (
    TracebackMapper,
    build_project_traceback_mapper,
    parse_traceback_frames,
)


def _write(path: Path, text: str = "print('ok')\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_parse_traceback_frames_and_map_remote_to_local_path(tmp_path: Path):
    source = tmp_path / "src" / "lib" / "sensor.py"
    _write(source)
    mapper = TracebackMapper.from_entries(
        [(str(source), "/lib/sensor.py")],
        local_base=tmp_path,
    )
    text = (
        "Traceback (most recent call last):\n"
        '  File "/lib/sensor.py", line 24, in read\n'
        "NameError: name 'Pin' isn't defined\n"
    )

    frames = parse_traceback_frames(text)
    mapped = mapper.map_text(text)

    assert frames[0].path == "/lib/sensor.py"
    assert frames[0].line == 24
    assert frames[0].function == "read"
    assert "/lib/sensor.py:24 -> src/lib/sensor.py:24" in mapped
    assert "NameError: name 'Pin' isn't defined" in mapped


def test_manifest_remote_remap_is_used_for_traceback_mapping(tmp_path: Path):
    source = tmp_path / "src" / "lib" / "sensor.py"
    manifest = tmp_path / "manifest.py"
    _write(source)
    _write(manifest, 'module("src/lib/sensor.py", remote="/lib/sensor.py")\n')
    mapper = build_project_traceback_mapper(
        str(tmp_path),
        "/",
        manifest_path=str(manifest),
    )

    mapped = mapper.map_text('  File "/lib/sensor.py", line 7, in sample\n')

    assert "/lib/sensor.py:7 -> src/lib/sensor.py:7" in mapped


def test_mpy_traceback_points_to_source_without_faking_local_line(tmp_path: Path):
    source = tmp_path / "src" / "lib" / "sensor.py"
    _write(source)
    mapper = TracebackMapper.from_entries(
        [(str(source), "/lib/sensor.py")],
        local_base=tmp_path,
        auto_compile=True,
    )

    mapped = mapper.map_text('  File "/lib/sensor.mpy", line 3, in read\n')

    assert (
        "/lib/sensor.mpy:3 -> src/lib/sensor.py "
        "(.mpy bytecode; source line unavailable)"
    ) in mapped
    assert "src/lib/sensor.py:3" not in mapped


def test_unmatched_traceback_output_is_left_unchanged(tmp_path: Path):
    source = tmp_path / "main.py"
    _write(source)
    mapper = TracebackMapper.from_entries(
        [(str(source), "/main.py")],
        local_base=tmp_path,
    )
    text = (
        "Traceback (most recent call last):\n"
        '  File "/other.py", line 2, in <module>\n'
        "ValueError: bad\n"
    )

    assert mapper.map_text(text) == text


def test_suffix_rule_maps_common_src_prefix_without_manifest(tmp_path: Path):
    source = tmp_path / "src" / "lib" / "sensor.py"
    _write(source)
    mapper = build_project_traceback_mapper(str(tmp_path), "/")

    mapped = mapper.map_text('  File "/lib/sensor.py", line 11, in read\n')

    assert "/lib/sensor.py:11 -> src/lib/sensor.py:11" in mapped
