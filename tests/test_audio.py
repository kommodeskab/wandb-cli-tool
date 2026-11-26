import stat
import struct
import wave
from pathlib import Path

import pytest
from textual.widgets import Select, Static

from chart_helpers import new_panel, settle, start_add
from conftest import FakeClient, FakePlayer
from test_run_screens import open_charts
from wandb_cli_tool import config
from wandb_cli_tool.app import WandbCliApp
from wandb_cli_tool.audio import (
    AudioInfo, audio_entries, columns_for, format_time, parse_audio_steps, read_waveform, waveform_rows,
)
from wandb_cli_tool.charts import AUDIO, ChartSpec
from wandb_cli_tool.config import Credentials
from wandb_cli_tool.images import ImageSeries, ImageStep
from wandb_cli_tool.player import AudioPlayer
from wandb_cli_tool.screens.audio_widgets import AudioChartView, AudioPlayerView
from wandb_cli_tool.screens.chart_dialogs import AudioDialog

SIZE = (130, 50)


def write_wav(path: Path, samples: list[int], rate: int = 8000, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels), w.setsampwidth(2), w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


# ---- pure ----------------------------------------------------------------------------------


def test_audio_keys_and_steps_come_from_the_logged_records():
    one = {"_type": "audio-file", "path": "media/audio/a_1_x.wav", "sha256": "s", "size": 10, "caption": "hi"}
    many = {"_type": "audio", "audio": [one, {**one, "path": "media/audio/a_1_y.wav"}]}
    assert audio_entries({"a": one, "b": many, "c": 1.0, "d": {"_type": "image-file", "path": "x.png"}}) == [
        AudioInfo("a", False, 1), AudioInfo("b", True, 2),
    ]
    series = parse_audio_steps([{"_step": 3, "b": many}, {"_step": 1, "b": many}, {"_step": 2}, {"_step": 4, "b": "junk"}], "b")
    assert [s.step for s in series.steps] == [1, 3] and len(series.steps[0].paths) == 2
    assert series.steps[0].caption(0) == "hi" and parse_audio_steps([{"_step": 1, "a": one}], "a").steps[0].caption(0) == "hi"


def test_a_wav_gives_its_duration_and_normalised_loudness(tmp_path):
    wav = write_wav(tmp_path / "a.wav", [0] * 4000 + [20000, -20000] * 2000)  # 1 s: silence, then loud
    wf = read_waveform(wav)
    assert wf.duration == pytest.approx(1.0) and max(wf.peaks) == 1.0
    cols = columns_for(wf.peaks, 10)
    assert cols[0] == 0.0 and cols[-1] == 1.0 and len(cols) == 10


def test_an_unreadable_file_is_an_error_not_a_crash(tmp_path, monkeypatch):
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"not audio")
    monkeypatch.setattr("shutil.which", lambda name: None)  # no ffmpeg
    with pytest.raises(ValueError):
        read_waveform(bad)


def test_waveform_rows_are_mirrored_and_a_silent_clip_is_a_thin_line():
    rows = waveform_rows([0.0, 1.0], 4)
    assert len(rows) == 4 and all(len(r) == 2 for r in rows)
    assert [r[1] for r in rows] == ["█"] * 4 and rows[1][0] == "▄" and rows[2][0] == "▀" and rows[0][0] == " "


def test_times():
    assert format_time(0) == "0:00" and format_time(75.9) == "1:15" and format_time(3725) == "1:02:05"


# ---- the player: pause and continue really stop and restart the process -------------------------


def test_pause_freezes_the_process_and_the_position_stands_still(tmp_path):
    script = tmp_path / "fakeplayer"
    script.write_text("#!/bin/sh\nsleep 30\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    player = AudioPlayer(which=lambda exe: str(script) if exe == "mpv" else None)
    clip = tmp_path / "c.wav"
    try:
        assert player.play(clip) and player.is_playing(clip) and not player.paused
        assert player.pause() and player.paused and player.playing
        frozen = player.position
        import time

        time.sleep(0.2)
        assert player.position == frozen  # time does not run while paused
        assert not player.pause()  # already paused
        assert player.resume() and not player.paused and player.is_playing(clip)
        time.sleep(0.2)
        assert player.position >= frozen + 0.15
    finally:
        player.stop()  # also frees a paused process
    assert not player.playing and player.position == 0.0
    assert not player.is_playing(clip)


# ---- on screen -----------------------------------------------------------------------------


class WithAudio(FakeClient):
    IMAGES: list = []
    AUDIOS = [AudioInfo("clips", True, 2)]
    AUDIO_STEPS = {
        "clips": [
            ImageStep(s, tuple(f"media/audio/clips_{s}_{i}.wav" for i in range(2)), (), (), ("one", "two"))
            for s in (1, 2, 3)
        ]
    }


def bar_text(app) -> str:
    from wandb_cli_tool.screens.keybar import KeyBar

    return app.screen.query_one(KeyBar).query_one("KeyLine").render().plain.replace("\xa0", " ")


async def test_an_audio_chart_shows_a_waveform_and_can_be_played_paused_and_continued():
    config.save(Credentials("k", "me"))
    player = FakePlayer()
    app = WandbCliApp(client_factory=WithAudio, audio_player=player)
    async with app.run_test(size=SIZE) as pilot:
        screen = await open_charts(pilot)
        assert any(label.startswith("Audio (1)") for label in [str(o.prompt) for o in screen.query_one("#panels").options])
        await pilot.press(*["down"] * 5)  # the generated Audio panel is last
        await settle(pilot, 6)
        view = screen.query(AudioChartView).first()
        assert view.spec == ChartSpec("clips", kind=AUDIO) and view.spec.title == "Audio: clips"
        view.focus()
        await settle(pilot, 4)
        assert view.current_step.step == 3 and view.sub == 0  # newest first
        assert view.player_view.path is not None and view.player_view.waveform is not None
        text = " ".join(str(w.render()) for w in view.query(Static)) + bar_text(app)
        assert "step 3" in text and "one" in text and "space Play" in bar_text(app)
        await pilot.press("space")
        await settle(pilot)
        assert player.playing and player.played[-1].name.startswith("clips_3_0") and "space Pause" in bar_text(app)
        await pilot.press("space")
        await settle(pilot)
        assert player.paused and "space Continue" in bar_text(app)
        await pilot.press("space")
        await settle(pilot)
        assert player.playing and not player.paused and len(player.played) == 1
        await pilot.press("left")  # another step: the old clip stops
        await settle(pilot, 4)
        assert not player.playing and view.current_step.step == 2
        await pilot.press("right_square_bracket")  # the second clip of that step
        await settle(pilot, 4)
        assert view.sub == 1 and "two" in str(view.query_one("#audio-info", Static).render())
        await pilot.press("space")
        await settle(pilot)
        assert player.playing
        await pilot.press("left")  # leaves the chart's clip
        await pilot.press("shift+tab")
        await settle(pilot, 3)
        await pilot.press("up")  # (focus on the panels list) switch panel: the chart goes away and so does the sound
        await settle(pilot, 6)
        assert not player.playing


async def test_audio_can_be_added_from_the_kind_dialog_and_an_unlogged_key_is_a_note():
    config.save(Credentials("k", "me"))
    app = WandbCliApp(client_factory=WithAudio, audio_player=FakePlayer())
    async with app.run_test(size=SIZE) as pilot:
        screen = await open_charts(pilot)
        await new_panel(pilot, "mine")
        await pilot.press("a")
        await settle(pilot)
        await pilot.press("down", "down", "down", "enter")  # (images are not available here: skipped)
        await settle(pilot)
        dialog = app.screen
        assert isinstance(dialog, AudioDialog)
        dialog.query_one("#audio-key", Select).value = "clips"
        await pilot.click("#ok")
        await settle(pilot, 6)
        view = screen.query(AudioChartView).first()
        assert view.spec.y == "clips" and view.current_step is not None
        screen.chart_layout.add_chart(screen.selected, ChartSpec("gone", kind=AUDIO))
        await screen._show_panel()
        await settle(pilot, 5)
        gone = [v for v in screen.query(AudioChartView) if v.spec.y == "gone"][0]
        assert gone.unavailable and "no logged audio called" in gone.unavailable


async def test_audio_without_a_player_says_what_to_install_when_play_is_pressed():
    from wandb_cli_tool.player import INSTALL_HINT

    config.save(Credentials("k", "me"))
    app = WandbCliApp(client_factory=WithAudio, audio_player=FakePlayer(available=False))
    async with app.run_test(size=SIZE) as pilot:
        screen = await open_charts(pilot)
        await pilot.press(*["down"] * 5)
        await settle(pilot, 6)
        screen.query(AudioChartView).first().focus()
        await settle(pilot, 3)
        await pilot.press("space")
        await settle(pilot)
        assert any(INSTALL_HINT in n.message for n in app._notifications)


# ---- when nothing can be played (e.g. over SSH) ------------------------------------------------


def test_a_player_that_fails_reports_why(tmp_path):
    script = tmp_path / "fakeplayer"
    script.write_text("#!/bin/sh\necho 'no audio device' >&2\nexit 1\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    player = AudioPlayer(which=lambda exe: str(script) if exe == "mpv" else None)
    assert player.play(tmp_path / "c.wav")
    import time

    for _ in range(50):
        if player.check_finished():
            break
        time.sleep(0.05)
    assert player.failure == "no audio device" and not player.playing
    assert not player.check_finished()  # reported once


def test_playback_problem_mentions_ssh_only_over_ssh(monkeypatch):
    from wandb_cli_tool.screens.audio_widgets import playback_problem

    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    assert "SSH" not in playback_problem("boom", 0.1) and "boom" in playback_problem("boom", 0.1)
    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
    assert "over SSH" in playback_problem(None, 0.25) and "0.2 s" in playback_problem(None, 0.25)


async def test_a_clip_that_dies_early_is_reported_and_the_view_goes_back_to_stopped(tmp_path):
    from wandb_cli_tool.screens.files import FileScreen
    from wandb_cli_tool.api import FileInfo, RunRef

    config.save(Credentials("k", "me"))
    wav = write_wav(tmp_path / "long.wav", [1000] * 16000)  # 2 s
    player = FakePlayer()
    app = WandbCliApp(client_factory=FakeClient, audio_player=player)
    async with app.run_test(size=SIZE) as pilot:
        view = AudioPlayerView()
        await app.mount(view)
        await settle(pilot)
        from wandb_cli_tool.audio import read_waveform

        view.set_clip(wav, read_waveform(wav))
        player.play(wav)
        player.position = 0.1
        await pilot.pause(0.3)
        assert view.state == "playing"
        player.failure, player.finish_next = "no audio device", True  # the player process died
        await pilot.pause(0.4)
        assert view.state == "stopped" and "no audio device" in view.problem
        assert any("no audio device" in n.message for n in app._notifications)


async def test_a_player_that_lingers_after_the_end_of_the_clip_is_stopped(tmp_path):
    config.save(Credentials("k", "me"))
    wav = write_wav(tmp_path / "short.wav", [1000] * 800)  # 0.1 s
    player = FakePlayer()
    app = WandbCliApp(client_factory=FakeClient, audio_player=player)
    async with app.run_test(size=SIZE) as pilot:
        view = AudioPlayerView()
        await app.mount(view)
        from wandb_cli_tool.audio import read_waveform

        view.set_clip(wav, read_waveform(wav))
        player.play(wav)
        player.position = 5.0  # long past the end, yet "still playing"
        await pilot.pause(0.4)
        assert not player.playing and view.state == "stopped" and not view.problem


# ---- comparing audio ---------------------------------------------------------------------------


class AudioCompareClient(__import__("test_compare_screen").CompareClient):
    """id1 and id3 logged audio (id2 did not)."""

    def run_audios(self, ref):
        return [AudioInfo("clips")] if ref.id != "id2" else []

    def audio_steps(self, ref, key):
        numbers = {"id1": (10, 20, 30), "id3": (20, 40)}[ref.id]
        return ImageSeries(
            tuple(ImageStep(n, (f"media/audio/{ref.id}_{n}.wav",), (f"sha{ref.id}{n}",), (None,), (f"cap{n}",)) for n in numbers),
            len(numbers),
        )


async def test_compared_audio_shows_waveforms_side_by_side_and_plays_one_run_at_a_time():
    from test_compare_screen import compare, show_panel
    from wandb_cli_tool.charts import Customization
    from wandb_cli_tool.layout_store import project_key
    from wandb_cli_tool.screens.compare_widgets import CompareAudioView

    config.save(Credentials("k", "me"))
    player = FakePlayer()
    app = WandbCliApp(client_factory=AudioCompareClient, audio_player=player)
    app.layout_store.save(project_key("me", "alpha"), Customization(
        panels=["Sounds"], added={"Sounds": [ChartSpec("clips", kind=AUDIO)]}))
    async with app.run_test(size=(150, 50)) as pilot:
        screen = await compare(pilot, 0, 1, 2)
        await show_panel(pilot, screen, "Sounds")
        [view] = screen.query(CompareAudioView)
        assert view.steps == [10, 20, 30, 40] and view.target == 40
        assert view.step_for(0).step == 30 and view.step_for(1) is None and view.step_for(2).step == 40
        labels = [l.render().plain for l in view.query(".cell-label")]
        assert "step 30" in labels[0] and "not logged" in labels[1] and "step 40" in labels[2]
        assert labels[0].startswith("♪") and not labels[2].startswith("♪")  # listening to the first run
        players = view.players()
        assert players[0].waveform is not None and players[2].waveform is not None and players[1].path is None
        assert sorted(app.client.downloads) == ["media/audio/id1_30.wav", "media/audio/id3_40.wav"]
        view.focus()
        await settle(pilot, 3)
        assert "space Play" in bar_text(app) and "l Listen to: run-1" in bar_text(app).replace("  ", " ")
        await pilot.press("space")
        await settle(pilot)
        assert player.playing and player.played[-1].name == "id1_30.wav"
        await pilot.press("l")  # switch to the other run: the playing clip is replaced, never mixed
        await settle(pilot)
        assert view.listen == 2 and player.playing and player.played[-1].name == "id3_40.wav" and len(player.played) == 2
        assert "Listen to: run-3" in bar_text(app)
        await pilot.press("left")  # another step: playback stops
        await settle(pilot, 6)
        assert not player.playing and view.target == 30
        assert view.runs_with_clips() == [0, 2]  # run-2 has no clip: never listened to
        await pilot.press("l")
        assert view.listen == 0  # round to the first again, skipping run-2
