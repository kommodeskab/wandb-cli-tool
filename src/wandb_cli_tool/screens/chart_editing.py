"""Adding, changing and deleting the charts of the project's layout, for every screen that shows it
(one run, or a comparison of runs). The layout belongs to the project, so an edit made on either
screen is seen on the other.

A screen using this provides: `ready`, `chart_layout`, `selected`, `panels`, `metrics`,
`system_metrics`, `tables`, `images`, `_layout_key`, `empty_note`, `_focused_chart()`,
`_refresh_panel_list()`, `_show_panel(focus)`, `_update_status()` and `action_focus_panels()`.
`focus` and the chart indexes used here are positions in the LAYOUT's panel (not in what is drawn:
a comparison draws a multi-metric plot as several charts)."""

from __future__ import annotations

from ..charts import BAR, IMAGE, PLOT, TABLE, ChartSpec
from .chart_dialogs import (
    AxisDialog, BarDialog, ChartKindDialog, ChartOptionsDialog, ImageDialog, PlotDialog, TableDialog,
)
from .dialogs import ConfirmDialog, NewPanelDialog

EDIT_ACTIONS = ("new_panel", "add_chart", "change_x", "chart_options", "delete_chart", "reset_layout")


class ChartEditing:
    empty_note = "This run has no metrics, tables or images to chart."

    def check_chart_action(self, action: str) -> bool | None:
        """Whether an editing action does something right now (the key bar lists only those)."""
        focused = self._focused_chart()
        if action in ("change_x", "delete_chart"):
            return focused is not None
        if action == "chart_options":
            return focused is not None and focused[0].kind in (PLOT, BAR)
        if action == "reset_layout":
            return bool(self.chart_layout and self.chart_layout.is_customized)
        return True

    def _save_layout(self) -> None:
        """Persist the customizations right away, so nothing is lost if the app is killed."""
        if not self.app.layout_store.save(self._layout_key, self.chart_layout.custom):
            self.notify("Could not save the chart layout (it still works for this session).", severity="warning")
        self._update_status()

    def action_new_panel(self) -> None:
        if self.ready:
            self.app.push_screen(NewPanelDialog(), self._on_new_panel)

    async def _on_new_panel(self, name: str | None) -> None:
        if not name:
            return
        self.selected = self.chart_layout.add_panel(name)
        self._save_layout()
        self._refresh_panel_list()
        await self._show_panel()

    def action_add_chart(self) -> None:
        """Step 1: what kind of chart? Step 2 (below) asks for that kind's details."""
        if not self.ready:
            return
        values = len(self.metrics) + len(self.system_metrics)
        if not (values or self.tables or self.images):
            self.notify(self.empty_note, severity="warning")
            return
        self.app.push_screen(
            ChartKindDialog(
                plot_ok=values >= 1,
                bar_ok=values >= 2,
                table_ok=bool(self.tables),
                image_ok=bool(self.images),
            ),
            self._on_kind,
        )

    def _on_kind(self, kind: str | None) -> None:
        if kind == PLOT:
            self.app.push_screen(PlotDialog(self.metrics, self.system_metrics), self._on_add_chart)
        elif kind == BAR:
            self.app.push_screen(BarDialog(self.metrics, self.system_metrics), self._on_add_chart)
        elif kind == TABLE:
            self.app.push_screen(TableDialog(self.tables), self._on_add_chart)
        elif kind == IMAGE:
            self.app.push_screen(ImageDialog(self.images), self._on_add_chart)

    async def _on_add_chart(self, spec: ChartSpec | None) -> None:
        if not spec:
            return
        index = self.chart_layout.add_chart(self.selected, spec)
        self._save_layout()
        self._refresh_panel_list()
        await self._show_panel(focus=index)

    def action_change_x(self) -> None:
        focused = self._focused_chart()
        if focused is None:
            return
        spec, index = focused
        if spec.kind != PLOT:
            self.notify("The x-axis only applies to plots.")
            return
        self.app.push_screen(AxisDialog(self.metrics, spec), lambda new: self._on_change_x(index, new))

    def action_chart_options(self) -> None:
        focused = self._focused_chart()
        if focused is not None and focused[0].kind in (PLOT, BAR):
            spec, index = focused
            self.app.push_screen(ChartOptionsDialog(spec), lambda new: self._on_change_x(index, new))  # a changed spec

    async def _on_change_x(self, index: int, spec: ChartSpec | None) -> None:
        if not spec:
            return
        self.chart_layout.set_chart(self.selected, index, spec)
        self._save_layout()
        await self._show_panel(focus=index)

    def action_delete_chart(self) -> None:
        """Delete the focused chart or table from this panel (after confirming)."""
        focused = self._focused_chart()
        if not self.ready or focused is None:
            return
        spec, index = focused
        self.app.push_screen(
            ConfirmDialog("Delete this chart?", f"“{spec.title}”", yes="Yes, delete", no="No"),
            lambda yes: self._on_delete(index, yes),
        )

    async def _on_delete(self, index: int, yes: bool | None) -> None:
        if not yes:
            return
        self.chart_layout.remove_chart(self.selected, index)
        self._save_layout()
        self._refresh_panel_list()
        remaining = len(self.panels[self.selected].charts)
        await self._show_panel(focus=min(index, remaining - 1) if remaining else None)
        if not remaining:
            self.action_focus_panels()  # nothing left to focus in this panel

    def action_reset_layout(self) -> None:
        """Forget all panels, charts and x-axis choices made in this project (after confirming)."""
        if not self.ready:
            return
        if not self.chart_layout.is_customized:
            self.notify("This project already uses the default layout.")
            return
        self.app.push_screen(
            ConfirmDialog(
                "Back to the default layout?",
                "This removes the panels and charts you added, brings back the charts\n"
                "you deleted, and undoes axis and option changes.",
                yes="Yes, reset", no="No",
            ),
            self._on_reset,
        )

    async def _on_reset(self, yes: bool | None) -> None:
        if not yes:
            return
        self.chart_layout.reset()
        self.app.layout_store.reset(self._layout_key)
        self.selected = 0
        self._update_status()
        self._refresh_panel_list()
        await self._show_panel()
        self.notify("Charts reset to the default layout.")
