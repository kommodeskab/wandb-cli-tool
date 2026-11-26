# wandb-cli-tool

Browse your [Weights & Biases](https://wandb.ai) projects, runs, charts and files from the terminal.

## Install

Requires [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/kommodeskab/wandb-cli-tool.git
cd wandb-cli-tool
uv tool install .
```

## Start

```
wandb-cli-tool
```

Enter your API key ([wandb.ai/authorize](https://wandb.ai/authorize)), pick an entity, and go. Arrow keys move, `Enter` opens, `Esc` goes back, `q` quits. The key bar at the bottom shows what each key does.

Images in VS Code's terminal need `terminal.integrated.enableImages` and `terminal.integrated.gpuAcceleration` turned on.

## Develop

```
uv sync && uv run pytest
```
