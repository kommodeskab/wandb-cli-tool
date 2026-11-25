import json
import stat

from wandb_cli_tool.jsonfile import atomic_write_json, read_versioned


def test_read_versioned_returns_the_inner_dict_or_empty_for_anything_unusable(tmp_path):
    p = tmp_path / "f.json"
    assert read_versioned(p, 1, "values") == {}  # missing
    p.write_text("{nope")
    assert read_versioned(p, 1, "values") == {}  # damaged
    p.write_text(json.dumps({"version": 2, "values": {"a": 1}}))
    assert read_versioned(p, 1, "values") == {}  # another format version
    p.write_text(json.dumps({"version": 1, "values": [1]}))
    assert read_versioned(p, 1, "values") == {}  # field is not a dict
    p.write_text(json.dumps([1, 2]))
    assert read_versioned(p, 1, "values") == {}  # not even an object
    p.write_text(json.dumps({"version": 1, "values": {"a": 1}}))
    assert read_versioned(p, 1, "values") == {"a": 1}


def test_read_versioned_returns_a_copy(tmp_path):
    p = tmp_path / "f.json"
    p.write_text(json.dumps({"version": 1, "values": {"a": 1}}))
    first = read_versioned(p, 1, "values")
    first["a"] = 99
    assert read_versioned(p, 1, "values") == {"a": 1}


def test_atomic_write_is_private_leaves_no_temp_files_and_replaces_in_one_step(tmp_path):
    p = tmp_path / "sub" / "f.json"
    assert atomic_write_json(p, {"a": [1, 2]}) and json.loads(p.read_text()) == {"a": [1, 2]}
    assert stat.S_IMODE(p.stat().st_mode) == 0o600 and not list(tmp_path.rglob("*.tmp"))
    assert atomic_write_json(p, {"b": 1}) and json.loads(p.read_text()) == {"b": 1}


def test_atomic_write_can_make_the_folder_private_too(tmp_path):
    p = tmp_path / "private" / "f.json"
    assert atomic_write_json(p, 1, dir_mode=0o700)
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_atomic_write_reports_failure_without_raising_or_touching_the_old_file(tmp_path):
    p = tmp_path / "f.json"
    atomic_write_json(p, {"keep": 1})
    assert atomic_write_json(p, {"bad": object()}) is False  # not JSON
    assert json.loads(p.read_text()) == {"keep": 1} and not list(tmp_path.glob("*.tmp"))
    (tmp_path / "file").write_text("a file, not a folder")
    assert atomic_write_json(tmp_path / "file" / "f.json", 1) is False
