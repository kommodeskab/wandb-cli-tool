# wandb-cli-tool

A keyboard-driven terminal UI for browsing [Weights & Biases](https://wandb.ai): projects, runs, logs, charts, config, and logged files (images, audio, text). Almost everything is read-only; the only edits are to runs: group, tags, name, copy, stop and delete.

## Install and run

```
uv sync
uv run wandb-cli-tool          # or: uv tool install .   and then just: wandb-cli-tool
```

On first launch you are asked for your API key and then pick an entity. Navigate with the arrow keys, `Enter` to open, `Esc` to go back, `q` to quit. In the projects list, `p` pins a project to the top (marked ★, remembered per entity; `p` again unpins). The key bar at the bottom shows the keys that apply right now (a key that switches something shows its state: ☑ on, ☐ off, or the value in use, e.g. `Auto-refresh: 30 s`) (it wraps onto more lines on a narrow terminal). Logout (`l`), change entity (`e`) and quit (`q`) are listed on the projects page only, but work everywhere.

## Sorting runs

In a project's run list, `s` opens a sort menu: last activity (the default, newest first), name, status, started, run time, group, tags, or ID. Choosing the column that is already in use reverses it; clicking a column header does the same. Names sort naturally (`run-2` before `run-10`), run time sorts by the actual duration, and empty values always come last. The current sort is shown in the status line and as an arrow in the column header, and it is remembered for next time.

## Comparing runs

In a project's run list, press `Space` to select the run under the cursor (a ✓ appears; `Space` again unselects, `Ctrl+A` selects all runs the filter shows, `u` clears the selection). Select 2 to 8 runs of the same project and press `c` to compare them. Each run gets a color that follows it everywhere (shown in the legend at the top).

- **Overview** (`1`): the facts from the top of a run screen (status, start, run time, group, tags, host, GPU, git, command, ID), one column per run. Rows where the runs differ are marked `≠`; `i` hides the identical rows.
- **Config** (`3`): only what differs. A row is an entry (nested keys are written `model.name`, list items `layers[0]`) with each run's value; `—` means that run's config lacks the entry, and entries only some runs have are yellow. Entries that are the same in every run are hidden (`d` shows them), and the status line says how many there are.
- **Metrics** (`2`): the latest value of every metric, one column per run, with a **Best** column naming the best run of each metric (green). Lower is better by default; press `b` to flip the metric under the cursor to "higher is better" (and back), or `B` to flip all. The **Better** column shows the direction, and your choices are remembered for the project. `i` hides rows where all runs agree.
- **Charts** (`4`): the project's chart layout over all the selected runs. Charts and panels are added, changed and deleted here with the same keys as in a single run (`n` new panel, `a` add chart, `o` chart options, `Delete`, `d` back to the default layout), and the layout is the project's one layout, so every change shows in the single-run screens too (and the other way round). The metrics, tables and images you can choose from are those of all the selected runs. A plot overlays one curve per run. A plot with several metrics is split into one plot per metric, so the lines stay readable. A bar chart draws, for each metric, one bar per run side by side. A plot where every run logged a single value is drawn as bars instead. Runs that did not log a metric are named in the chart and never queried. A table is shown as one table with a colored Run column (`v` lines up rows with the same first column), and images are shown side by side at the same step with one slider for all runs.

In the Metrics and Config tabs, `/` filters the rows by key as you type (every word must occur in the key; `Enter` keeps the filter, `Esc` clears it; each tab keeps its own), and `p` pins the metric or config key under the cursor to the top (marked ★; a pinned key is always listed, even when its values are identical). Pins are remembered per project.

Logs and files are not compared. Comparing only reads data; it changes nothing in W&B.

## Finding and watching runs

- `p` pins the selected runs (or the run under the cursor) to the top of the list, in the order you pinned them, whatever the sort; `p` again unpins. Pins are remembered per project.
- `Ctrl+A` (hold the Control key and press A; it is written `^a` in some terminals) selects every run the filter currently shows. If your terminal multiplexer uses Ctrl+A as its prefix (tmux/screen), press it twice or use Space on each run.
- `/` filters the run list as you type: every word must occur in the run's name, status, group, tags or id (case does not matter). `Enter` keeps the filter and returns to the list, `Esc` clears it.
- While the list is open and some run is running, it checks for changes every 30 seconds and tells you when a run finishes, fails or a new one starts (a terminal beep for runs that did not finish well). The status line says how many it is watching.
- `o` opens the run under the cursor (or the open run) in your browser. It uses `$BROWSER` if set (VS Code's remote terminals set it, so the link opens in the browser on your own machine), and otherwise the system's default on macOS/Windows, or `xdg-open` when there is a display. Where no browser can be opened (a bare SSH session), the link is copied to the clipboard through the terminal and shown instead.
- The log of a running run is followed: new lines appear every 5 seconds (`f` stops/starts it, `r` reloads everything).

## Managing runs

In the run screen: `s` stops a running run (it asks the run to shut down gracefully, like the Stop button in W&B) and `Delete` deletes the run. In the run list, `a` opens a menu of actions for the selected runs (or the run under the cursor): stop the running ones, set group, add tags, delete.

- **Delete** asks you to type the word `delete`; Enter or `y` alone never deletes anything. Metrics, logs, config and files of the run are gone for good. The artifacts it logged are **kept** unless you tick "Also delete the artifacts these runs logged": then you must type `delete artifacts` instead, and the artifacts are removed from W&B too, even if other runs or collections use them.
- **Stop** is a request: it only works if the run's process is still alive and listening.
- Moving a run to another project is not offered: W&B has no public API for it.

## Editing runs

On a run's screen: `g` sets its group, `t` adds tags, `r` renames it, `c` copies it. These are the only things the app writes to W&B.

**Copy** asks for a yes/no confirmation (showing roughly how much will be uploaded), then creates a new run named `<name> (copy)` in the same project and shows progress; `Esc` cancels. W&B has no copy operation, so the copy is rebuilt by replaying the original:

- Copied: config, tags, group, notes, all metric history rows (steps kept), summary, files (including media), and your own artifacts (models, datasets) as new uploads. Artifacts the run only *used* are linked.
- Not copied: system metrics (CPU/GPU curves) and the original timestamps of history rows (the new rows are stamped with the time of copying). Reference artifacts and W&B-internal artifacts are skipped.
- Large runs take a while and need local disk space for the files being copied. The original run is never changed. If a copy fails or is cancelled, the partial run is left as a *failed* run so you can delete it in W&B.

## Charts

In a run's Charts tab (`3`), `n` creates a panel and `a` adds a chart to the selected panel. Any logged `wandb.Table` or `wandb.Image` keys also appear automatically in **Tables** and **Images** panels. `a` first asks what kind of chart:

- **Plot**: tick one or more values (up to 8, e.g. `train_loss` and `val_loss`) to draw them in one plot with a legend, and pick the x-axis they share. The selected metrics appear below in draw order (top draws on top); focus the order list and press `Ctrl+↑` / `Ctrl+↓` to reorder them. A filter box narrows the list.
- **Bar chart**: tick two or more metrics; each bar is that metric's latest value. They are drawn top to bottom in the order you pick them; to rearrange, focus the order list in the dialog and press `Ctrl+↑` / `Ctrl+↓`.
- **Table**: show a `wandb.Table` that the run logged (the latest version of the one you pick). Use the arrow keys inside the table and `Tab` to move to the next chart.

Series colors come from a fixed palette in a fixed order. The kind and its settings are saved with the rest of the layout.

- **Images**: browse images logged with `wandb.Image` (one or several per step) with a step slider. The newest step is shown first. `←` / `→` change step (`Shift` for ±10, `Home` / `End` for the first and last), you can also click the slider, and `[` / `]` switch between images logged together at one step. Images are downloaded as you go and shown as a screen-sized copy, so even very large pictures step quickly. See the VS Code note below for sharp images.

**Refreshing.** `r` refreshes the charts now. While a run is still running they also refresh on their own (every 30 s by default; `f` cycles 10 s / 30 s / 1 min / 5 min / off, and the choice is remembered). Charts update in place, so your focus, scroll position and table cursor stay, new metrics appear, and a slider that was at the newest step follows new images. A failed refresh keeps what is on screen and says so in the status line. Finished runs are not refreshed automatically.

**Layouts are shared by every run in a project.** The panels you create, the charts you add, the charts you delete and the x-axis choices you make are saved for the whole project (in `chart_layouts.json` in the config directory, `~/.config/wandb-cli-tool/` on Linux) as soon as you make them, so every run of that project shows them. The default charts are still generated from each run's own metrics, with your changes applied on top. Runs often logged different things, so a shared chart is handled gracefully when a run lacks its data:

- a plot draws the series that run has, notes the rest as "not logged in this run", and does not even request the missing ones;
- if a run has none of a chart's data (or never logged its x-axis, table or images), the chart shrinks to a short note instead of an empty box;
- a bar chart shows a dash for metrics the run lacks.

When a run later logs the missing data, a refresh brings the chart to life. The first time you change something, the app tells you the change applies to all runs in the project, and deleting a chart or resetting (below) says so too. While the tool is under development, saved layouts may occasionally be discarded after an update (layouts saved per run in earlier versions were dropped when this changed).

To edit a chart, focus it and press `o`: change its metrics, x-axis, y-range (min and/or max), smoothing (0 to 0.99, an exponential moving average as in W&B: 0.9 is heavy), and logarithmic y and/or x axes on a plot (points a log axis cannot show are left out, and the chart says how many); change metrics, bar order, and fixed value axis on a bar chart; or switch the key shown by a table or image chart. Settings are saved with the chart and shared by all runs of the project, like everything else about the layout (also in the compare screen).

To delete a chart, table or image, focus it (`→` or `Tab`), press `Delete` (or `Backspace`), and confirm. To delete a whole panel, focus the panel list and press `Delete` (or `Backspace`) and confirm. Deleted default charts and panels stay deleted next time (even when new metrics appear); `d` brings everything back after a yes/no confirmation. Tables are as tall as their rows need (up to 20 lines, then they scroll).

## Showing images in the VS Code terminal

Images in the Files screen are drawn with real pixels (sixel graphics) only if the terminal supports it. In VS Code's integrated terminal, **both of these settings must be turned on**:

```json
{
  "terminal.integrated.enableImages": true,
  "terminal.integrated.gpuAcceleration": "on"
}
```

- Put them in your **User** settings (Settings → search for each name, or "Preferences: Open User Settings (JSON)"). When using Remote-SSH, terminal drawing happens in your local VS Code window.
- Open a **new terminal** (or restart VS Code) after changing them.
- To check, open an image in the app. The status line should say `render: auto → sixel`. If it says `render: auto → halfcell … low resolution`, the terminal is not offering graphics and images fall back to coarse colored blocks.

Other terminals with sixel or kitty graphics support (kitty, WezTerm, foot, iTerm2, Windows Terminal, …) work without these settings.

Inside the image view: `+` / `-` / `0` zoom, `m` cycles the render mode.

## Development

```
uv run pytest
```

See `AGENTS.md` (and `.agents/rules/`) for the architecture.
