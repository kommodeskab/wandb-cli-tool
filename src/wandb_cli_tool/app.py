from __future__ import annotations

from typing import Callable

from textual.app import App
from textual.binding import Binding
from textual.screen import Screen

from . import config
from .browser import open_in_browser
from .api import CachedClient, DiskCache, WandbApiClient, WandbClient
from .config import Credentials
from .layout_store import LayoutStore
from .preferences import Preferences
from .player import AudioPlayer
from .screens.login import EntityScreen, LoginScreen
from .screens.projects import ProjectsScreen


class WandbCliApp(App):
    CSS = """
    #login, #entity-picker { width: 60; height: auto; margin: 2 4; }
    #entity-picker { border: round $accent; padding: 1 2; max-height: 80%; }
    #message, #status, #entity-status { color: $text-muted; padding: 0 1; }
    #items { height: 1fr; }
    #entities { height: auto; max-height: 20; }
    """
    # These work everywhere but are only listed on the projects page (ProjectsScreen), where they
    # matter; elsewhere they would only crowd the key bar.
    BINDINGS = [
        Binding("q", "quit", "Quit", show=False),
        Binding("e", "change_entity", "Change entity", show=False),
        Binding("l", "logout", "Logout", show=False),
    ]

    def __init__(
        self,
        client_factory: Callable[[str], WandbClient] | None = None,
        cache: DiskCache | None = None,
        audio_player: AudioPlayer | None = None,
        layout_store: LayoutStore | None = None,
        preferences: Preferences | None = None,
    ) -> None:
        super().__init__()
        self.preferences = preferences or Preferences()
        self.cache = cache or DiskCache()
        self.layout_store = layout_store or LayoutStore()
        self.audio_player = audio_player or AudioPlayer()
        # (entity, project) pairs whose run list is stale because a run was edited
        self.touched_projects: set[tuple[str, str]] = set()
        # (entity, project, run id) of runs deleted in this session: W&B's run listing can still return
        # a deleted run for a while, and the cached list still has it, so the lists hide these themselves
        self.deleted_runs: set[tuple[str, str, str]] = set()
        self.client_factory = client_factory or self._default_client
        self.creds: Credentials | None = None
        self.client: WandbClient

    def on_unmount(self) -> None:
        self.audio_player.stop()

    def notify(self, message, *, title="", severity="information", timeout=None, markup=False):  # noqa: ANN001
        """Notifications often carry names and server errors: never parse them as markup."""
        return super().notify(message, title=title, severity=severity, timeout=timeout, markup=False)

    def open_link(self, url: str) -> None:
        """Open a URL in the browser. Where that is not possible (an SSH session without a display or
        $BROWSER) the link is put on the clipboard through the terminal instead, and shown."""
        if open_in_browser(url):
            self.notify(f"Opening {url}")
        else:
            self.copy_to_clipboard(url)
            self.notify(f"No browser could be opened from here; link copied: {url}", severity="warning")

    def _default_client(self, api_key: str) -> WandbClient:
        return CachedClient(WandbApiClient(api_key), self.cache)

    def on_mount(self) -> None:
        creds = config.load()
        if creds and creds.entity:
            self._start(creds)
        else:
            self._show_login()

    def _show_login(self) -> None:
        self.creds = None
        self.push_screen(LoginScreen(), self._on_login)

    def _on_login(self, creds: Credentials | None) -> None:
        if creds:
            config.save(creds)
            self._start(creds)

    def _start(self, creds: Credentials) -> None:
        """Enter the logged-in state with a fresh screen stack."""
        self.creds = creds
        self.client = self.client_factory(creds.api_key)
        assert creds.entity
        self._reset_stack(ProjectsScreen(creds.entity))

    def _reset_stack(self, screen: Screen | None = None) -> None:
        while len(self.screen_stack) > 1:
            self.pop_screen()
        if screen is not None:
            self.push_screen(screen)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in ("change_entity", "logout") and self.creds is None:
            return None  # hidden/disabled while logged out
        return True

    def action_change_entity(self) -> None:
        if self.creds is None or isinstance(self.screen, EntityScreen):
            return
        self.push_screen(EntityScreen(self.creds.entity), self._on_entity)

    def _on_entity(self, entity: str | None) -> None:
        if entity and self.creds and entity != self.creds.entity:
            creds = Credentials(self.creds.api_key, entity)
            config.save(creds)
            self._start(creds)

    def action_logout(self) -> None:
        if self.creds is None:
            return
        config.clear()
        self.cache.clear()  # cached lists/data belong to the account that just left
        self._reset_stack()
        self._show_login()


def main() -> None:
    from .terminal import setup_cell_size

    setup_cell_size()  # must happen before Textual starts reading the keyboard
    WandbCliApp().run()
