import sys

from wandb_cli_tool import player as player_mod
from wandb_cli_tool.api.models import RunMetadata, parse_metadata
from wandb_cli_tool.formatting import fmt_size
from wandb_cli_tool.media import file_kind
from wandb_cli_tool.player import AudioPlayer


def test_parse_metadata_full_record():
    raw = {
        "host": "box-7",
        "git": {"remote": "git@github.com:me/r.git", "commit": "0123456789abcdef"},
        "program": "train.py",
        "args": ["--lr", "0.1", "--name", "my run"],
        "executable": "/usr/bin/python3.10",
        "gpu_nvidia": [{"name": "NVIDIA A100"}, {"name": "NVIDIA A100"}],
        "cpu_count": 16,
        "cpu_count_logical": 32,
        "python": "CPython 3.10.12",
    }
    meta = parse_metadata(raw, git_dirty=True)
    assert meta == RunMetadata(
        host="box-7",
        git_remote="git@github.com:me/r.git",
        git_commit="0123456789abcdef",
        git_dirty=True,
        command="python3.10 train.py --lr 0.1 --name 'my run'",  # shell-quoted
        gpu="NVIDIA A100 × 2",
        cpu_cores="16 (32 threads)",
        python="CPython 3.10.12",
    )


def test_parse_metadata_fallbacks_and_missing_data():
    assert parse_metadata(None) == RunMetadata()
    assert parse_metadata({}) == RunMetadata()
    legacy = parse_metadata({"gpu": "Tesla T4", "gpu_count": 1, "cpu_count": 4, "cpu_count_logical": 4})
    assert legacy.gpu == "Tesla T4" and legacy.cpu_cores == "4"
    assert parse_metadata({"gpu": "Tesla T4", "gpu_count": 0}).gpu == ""  # no GPUs used
    # dirty flag is meaningless without a commit
    assert parse_metadata({"git": {"remote": "x"}}, git_dirty=True).git_dirty is None
    # tolerate wrong types instead of crashing
    assert parse_metadata({"git": "oops", "args": "no", "program": "a.py", "gpu_nvidia": "?"}).command == "python a.py"


def test_file_kind_prefers_mimetype_then_extension():
    assert file_kind("x.bin", "image/png") == "image"
    assert file_kind("x.bin", "audio/wav") == "audio"
    assert file_kind("media/IMG.JPG") == "image"
    assert file_kind("a/b/clip.Mp3") == "audio"
    assert file_kind("output.log", "text/plain") == "text"
    assert file_kind("a.JSON") == file_kind("n.txt") == file_kind("x", "application/json") == "text"
    assert file_kind("model.ckpt") == file_kind("noext") == file_kind("a.bin", "application/zip") == "other"


def test_fmt_size():
    assert [fmt_size(n) for n in (None, 0, 812, 1536, 3 * 1024**2, 5 * 1024**3)] == [
        "—", "0 B", "812 B", "1.5 KB", "3.0 MB", "5.0 GB",
    ]


def test_player_picks_first_available_in_preference_order(tmp_path):
    installed = {"ffplay": "/usr/bin/ffplay", "aplay": "/usr/bin/aplay"}
    p = AudioPlayer(which=installed.get)
    cmd = p.command(tmp_path / "a.wav")
    assert cmd[0] == "/usr/bin/ffplay" and cmd[-1] == str(tmp_path / "a.wav") and "-nodisp" in cmd
    assert p.available
    none = AudioPlayer(which=lambda _: None)
    assert not none.available and none.command(tmp_path / "a.wav") is None
    assert none.play(tmp_path / "a.wav") is False


def test_player_plays_and_stops_a_real_process(monkeypatch, tmp_path):
    monkeypatch.setattr(
        player_mod, "PLAYERS", [(sys.executable, ["-c", "import time; time.sleep(30)"])]
    )
    p = AudioPlayer(which=lambda exe: exe)
    assert p.play(tmp_path / "a.wav") is True
    assert p.playing
    p.stop()
    assert not p.playing
    p.stop()  # idempotent
