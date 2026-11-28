"""Audio on screen: the waveform of a clip with a play position, and the chart that browses a key's clips."""

from __future__ import annotations

from pathlib import Path

from rich.console import Group, RenderResult
from rich.text import Text
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Static

from ..audio import Waveform, columns_for, format_time, progress_bar, waveform_rows
from ..charts import ChartSpec
from ..images import ImageSeries, StepCursor
from ..palette import series_color
from ..plain import plain
from .chart_widgets import DEBOUNCE, NOTE_HEIGHT, ChartBase, StepSlider, is_dark

AUDIO_CHART_HEIGHT = 16
TICK = 0.1  # seconds between redraws of the play position
END_GRACE = 0.7  # seconds after the end of a clip before a player that has not exited is stopped


def playback_problem(failure: str | None, played: float) -> str:
    """What to tell the user when a clip stopped by itself before its end (or the player failed)."""
    import os

    what = f"The audio could not be played ({failure})." if failure else f"The audio stopped after {played:.1f} s."
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        what += " Sound plays on the machine running this app, so over SSH nothing is heard on yours."
    else:
        what += " The machine running this app may have no audio output."
    return what


class AudioPlayerView(Widget):
    """The waveform of one clip, how far the listener is into it, and how long it is. The position
    follows `app.audio_player` while it plays this clip."""

    DEFAULT_CSS = "AudioPlayerView { height: 1fr; min-height: 5; }"

    class StateChanged(Message):
        """Playing / paused / stopped changed (a clip ended by itself): keys that depend on it must be redrawn."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.path: Path | None = None
        self.waveform: Waveform | None = None
        self.message = "Loading…"
        self._state = "stopped"
        self.problem = ""  # why the last attempt to play failed (shown in place of the position)
        self._last_position = 0.0
        self._wave_cache: tuple[tuple[int, int, int], list[str]] | None = None

    def on_mount(self) -> None:
        self.set_interval(TICK, self._tick)

    # ---- state ---------------------------------------------------------------------------
    def set_clip(self, path: Path, waveform: Waveform | None, note: str = "") -> None:
        """Show `path`. Without a waveform (it could not be read) only the player line is drawn, with `note`."""
        if path != self.path:
            self.problem = ""
        self.path, self.waveform, self.message = path, waveform, note
        self._wave_cache = None
        self.refresh()

    def clear(self, message: str) -> None:
        self.path, self.waveform, self.message = None, None, message
        self._wave_cache = None
        self.refresh()

    @property
    def state(self) -> str:
        player = self.app.audio_player
        if self.path is None or not player.is_playing(self.path):
            return "stopped"
        return "paused" if player.paused else "playing"

    @property
    def position(self) -> float:
        player = self.app.audio_player
        if self.path is not None and player.is_playing(self.path):
            self._last_position = player.position
            return self._last_position
        return 0.0

    def _tick(self) -> None:
        if self.path is None and self._state == "stopped":
            return
        player = self.app.audio_player
        mine = self.path is not None and player.path == self.path
        if mine:
            self._watch_end(player)
        state = self.state
        if state != self._state:
            self._state = state
            self.post_message(self.StateChanged())
            self.refresh()
        elif state == "playing":
            self.refresh()

    def _watch_end(self, player) -> None:
        """Notice that this clip's player has ended: a clip that is over but whose process lingers is
        stopped; one that ended early or with an error is reported (nothing may be audible, e.g. over SSH)."""
        duration = self.waveform.duration if self.waveform else 0.0
        if player.playing and not player.paused and duration and player.position > duration + END_GRACE:
            player.stop()  # the clip is over (some players do not exit by themselves without a sound device)
            return
        if not player.check_finished():
            return
        played = self._last_position
        if player.failure or (duration and played < min(duration * 0.9, duration - 0.3)):
            self.problem = playback_problem(player.failure, played)
            self.notify(self.problem, severity="warning", timeout=12)
            self.refresh()

    def play_started(self) -> None:
        self.problem = ""
        self._last_position = 0.0

    # ---- drawing -------------------------------------------------------------------------
    def render(self) -> RenderResult:
        width, height = self.size
        if self.path is None or width < 4 or height < 3:
            return Text(self.message, style="dim", no_wrap=True, overflow="ellipsis")
        accent = series_color(0, is_dark(self))
        duration = self.waveform.duration if self.waveform else 0.0
        position = min(self.position, duration) if duration else self.position
        fraction = position / duration if duration else 0.0
        done, _ = progress_bar(width, fraction)
        lines: list[Text] = []
        if self.waveform is not None and height >= 5:
            wave_key = (id(self.waveform), width, height - 2)
            if self._wave_cache is not None and self._wave_cache[0] == wave_key:
                wave_rows = self._wave_cache[1]
            else:
                wave_rows = waveform_rows(columns_for(self.waveform.peaks, width), height - 2)
                self._wave_cache = (wave_key, wave_rows)
            for row in wave_rows:
                text = Text(no_wrap=True, overflow="crop")
                text.append(row[:done], style=accent)
                text.append(row[done:], style="dim")
                lines.append(text)
        elif self.message:
            lines.append(Text(self.message, style="dim"))
        bar = Text(no_wrap=True, overflow="crop")
        knob = min(done, width - 1)
        bar.append("━" * knob, style=accent)
        bar.append("●", style=accent)
        bar.append("─" * (width - knob - 1), style="dim")
        lines.append(bar)
        icon = {"playing": "▶", "paused": "⏸", "stopped": "■"}[self.state]
        total = format_time(duration) if self.waveform else "?:??"
        line = Text(f"{icon} {format_time(position)} / {total}", no_wrap=True)
        if self.problem and self.state == "stopped":
            line.append("  " + self.problem, style="yellow")
        lines.append(line)
        return Group(*lines)


def play_label(state: str) -> str:
    return {"playing": "Pause", "paused": "Continue"}.get(state, "Play")


def toggle_playback(app, view: AudioPlayerView) -> str | None:
    """Play, pause or continue the clip shown in `view`. Returns a message when that is not possible."""
    player = app.audio_player
    path = view.path
    if path is None:
        return None
    state = view.state
    if state == "playing":
        if player.pause():
            return None
        player.stop()  # this platform cannot freeze the player: pausing stops it
        return None
    if state == "paused":
        return None if player.resume() else "Could not continue the audio."
    from ..player import INSTALL_HINT

    view.play_started()
    return None if player.play(path) else INSTALL_HINT


class AudioChartView(ChartBase, Vertical):
    """The audio clips logged under one key: a step slider, the waveform, and the play position."""

    can_focus = True
    BINDINGS = [
        Binding("space", "toggle_play", "Play"),
        Binding("left", "step(-1)", "Previous step"),
        Binding("right", "step(1)", "Next step"),
        Binding("shift+left", "step(-10)", "10 steps back", show=False),
        Binding("shift+right", "step(10)", "10 steps on", show=False),
        Binding("home", "jump_to(0)", "First step", show=False),
        Binding("end", "jump_to(-1)", "Last step", show=False),
        Binding("left_square_bracket", "sub(-1)", "Previous clip", key_display="["),
        Binding("right_square_bracket", "sub(1)", "Next clip", key_display="]"),
        Binding("up", "screen.focus_chart(-1)", "Prev chart", show=False),
        Binding("down", "screen.focus_chart(1)", "Next chart", show=False),
    ]
    DEFAULT_CSS = """
    AudioChartView { height: 16; border: round $primary; margin-bottom: 1; }
    AudioChartView:focus { border: round $accent; }
    AudioChartView #audio-info { height: 1; color: $text-muted; padding: 0 1; }
    AudioChartView AudioPlayerView { padding: 0 1; }
    """

    class Wanted(Message):
        """The clip for the current step should be fetched and shown."""

        def __init__(self, view: "AudioChartView") -> None:
            super().__init__()
            self.view = view

    def __init__(self, spec: ChartSpec, index: int) -> None:
        super().__init__()
        self._init_chart(spec, index)
        self.series: ImageSeries | None = None
        self.cursor = StepCursor(0)
        self.sub = 0  # which clip, when a step logged several
        self.waiting = True  # not "loading": Textual's Widget.loading shows a spinner and blocks focus
        self.error = ""
        self._wanted: tuple[int, int] | None = None
        self._shown: tuple[int, int] | None = None
        self._debounce = None
        self.unavailable: str | None = None

    def compose(self):
        yield AudioPlayerView(id="player")
        yield StepSlider(id="slider")
        yield Static("loading…", id="audio-info")

    def on_unmount(self) -> None:
        player = self.app.audio_player
        views = list(self.query(AudioPlayerView))
        if views and views[0].path is not None and player.is_playing(views[0].path):
            player.stop()  # the chart is gone (another panel, another tab): so is its sound

    # ---- keys ----------------------------------------------------------------------------
    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "sub":
            step = self.current_step
            return step is not None and len(step.paths) > 1
        if action == "toggle_play":
            views = list(self.query(AudioPlayerView))
            return bool(views) and views[0].path is not None and self.unavailable is None
        return True

    def key_label(self, action: str) -> str | None:
        if action == "toggle_play":
            views = list(self.query(AudioPlayerView))
            return play_label(views[0].state) if views else "Play"
        return None

    @property
    def player_view(self) -> AudioPlayerView:
        return self.query_one("#player", AudioPlayerView)

    def on_audio_player_view_state_changed(self, event: AudioPlayerView.StateChanged) -> None:
        event.stop()
        if self.is_attached:
            self.refresh_bindings()

    def action_toggle_play(self) -> None:
        message = toggle_playback(self.app, self.player_view)
        if message:
            self.notify(message, severity="warning")
        self.player_view.refresh()
        self.refresh_bindings()

    # ---- state ---------------------------------------------------------------------------
    @property
    def steps(self):
        return self.series.steps if self.series else ()

    @property
    def current_step(self):
        return self.steps[self.cursor.index] if self.steps else None

    def show_unavailable(self, message: str) -> None:
        """This run never logged this key: collapse to a short note."""
        if not self.is_attached:
            return
        self.unavailable = message
        self.player_view.display = False
        self.query_one("#slider").display = False
        self.styles.height = NOTE_HEIGHT
        self.border_subtitle = ""
        self.query_one("#audio-info", Static).update(plain(message))

    def _leave_unavailable(self) -> None:
        if self.unavailable is not None:
            self.unavailable = None
            self.player_view.display = True
            self.query_one("#slider").display = True
            self.styles.height = AUDIO_CHART_HEIGHT

    def set_steps(self, series: ImageSeries, keep_position: bool = False) -> None:
        """New list of steps. First time: show the newest. When refreshing: stay on the same
        step, unless the slider was at the end, in which case follow the newest."""
        if not self.is_attached:
            return
        self._leave_unavailable()
        previous = self.current_step.step if self.current_step else None
        was_end = self.cursor.at_end
        self.series = series
        n = len(series.steps)
        if not keep_position or was_end or previous is None:
            self.cursor = StepCursor(n, n - 1)
            if not keep_position:
                self.sub = 0
        else:
            nearest = min(range(n), key=lambda i: abs(series.steps[i].step - previous)) if n else 0
            self.cursor = StepCursor(n, nearest)
        step = self.current_step
        self.sub = min(self.sub, len(step.paths) - 1) if step else 0
        self.error = ""
        self._sync(immediate=True)

    def _key(self) -> tuple[int, int] | None:
        step = self.current_step
        return (step.step, self.sub) if step else None

    def _sync(self, immediate: bool = False) -> None:
        self.query_one("#slider", StepSlider).set_position(self.cursor.index, len(self.steps))
        key = self._key()
        if key is not None and key == self._shown and self.player_view.path is not None:
            self.waiting = False
        elif key is not None:
            self.waiting = True
            self._wanted = key
            player = self.app.audio_player
            if self.player_view.path is not None and player.is_playing(self.player_view.path):
                player.stop()  # another clip was chosen: the old one stops
            self._request(immediate)
        self._update_info()

    def _request(self, immediate: bool) -> None:
        if self._debounce is not None:
            self._debounce.stop()
            self._debounce = None
        if immediate:
            self.post_message(self.Wanted(self))
        else:
            self._debounce = self.set_timer(DEBOUNCE, lambda: self.post_message(self.Wanted(self)))

    def _update_info(self) -> None:
        step = self.current_step
        if self.series is None:
            text = "loading…"
        elif step is None:
            text = "No audio was logged under this key."
        elif self.error:
            text = f"step {step.step} · {self.cursor.index + 1}/{len(self.steps)} · Error: {self.error}"
        else:
            parts = [f"step {step.step}", f"{self.cursor.index + 1}/{len(self.steps)}"]
            if len(step.paths) > 1:
                parts.append(f"clip {self.sub + 1}/{len(step.paths)}")
            if self.waiting:
                parts.append("loading…")
            if step.caption(self.sub):
                parts.append(step.caption(self.sub))
            if self.series and self.series.sampled:
                parts.append(f"sampled: {len(self.steps):,} of {self.series.total:,} steps")
            text = " · ".join(parts)
        self.query_one("#audio-info", Static).update(plain(text))
        self.border_subtitle = ""
        self.refresh_bindings()

    # ---- results from the screen ---------------------------------------------------------
    def show_clip(self, path: Path, waveform: Waveform | None, key: tuple[int, int], note: str = "") -> None:
        if not self.is_attached or key != self._wanted:  # the slider has moved on or the view was removed
            return
        self._shown, self.waiting, self.error = key, False, ""
        self.player_view.set_clip(path, waveform, note)
        self._update_info()

    def fail(self, message: str, key: tuple[int, int] | None = None) -> None:
        if not self.is_attached or (key is not None and key != self._wanted):
            return
        self.error, self.waiting = message, False
        if self.series is None:
            self.query_one("#audio-info", Static).update(plain(f"Error: {message}"))
        else:
            self.player_view.clear(f"Error: {message}")
            self._update_info()

    # ---- keys ------------------------------------------------------------------------------
    def action_step(self, delta: int) -> None:
        if self.cursor.move(delta):
            self.sub = 0
            self._sync()

    def action_jump_to(self, index: int) -> None:
        if self.cursor.jump(self.cursor.n - 1 if index < 0 else index):
            self.sub = 0
            self._sync()

    def action_sub(self, delta: int) -> None:
        step = self.current_step
        if step is not None and 0 <= self.sub + delta < len(step.paths):
            self.sub += delta
            self._sync()

    def on_step_slider_jumped(self, event: StepSlider.Jumped) -> None:
        event.stop()
        if self.cursor.jump(event.index):
            self.sub = 0
            self._sync()
