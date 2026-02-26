# Contributing to MiloAgent

Thanks for your interest in contributing! Here's how you can help.

## Getting Started

1. **Fork** the repository
2. **Clone** your fork:
   ```bash
   git clone https://github.com/YOUR_USERNAME/MiloAgent.git
   cd MiloAgent
   ```
3. **Create a branch** for your feature:
   ```bash
   git checkout -b feature/my-awesome-feature
   ```
4. **Set up** the development environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

## What Can You Contribute?

### New Platforms
Add support for new social media platforms by implementing the `BasePlatform` interface in `platforms/base_platform.py`.

### New Personas
Add expert personas in `config/expert_personas.yaml` — each persona represents a domain expert with specific tone and knowledge.

### Prompt Templates
Improve or add LLM prompt templates in `prompts/` — these control how the bot generates content.

### Safety Improvements
Enhance anti-ban detection, rate limiting, or content validation in `safety/`.

### Dashboard Features
Add new views or metrics to the web dashboard (`dashboard/web.py`), TUI (`dashboard/tui.py`), or Telegram bot (`dashboard/telegram_bot.py`).

### Bug Fixes
Found a bug? Fix it and submit a PR!

## Code Style

- Python 3.10+ with type hints where helpful
- Use `async/await` for I/O operations
- Keep functions focused and under 50 lines when possible
- Add docstrings for public functions
- Follow existing patterns in the codebase

## Pull Request Process

1. Make sure your code works locally
2. Write clear commit messages
3. Describe what your PR does and why
4. Link any related issues

## Reporting Issues

When reporting a bug, please include:
- Python version (`python3 --version`)
- OS (macOS/Linux)
- Steps to reproduce
- Relevant log output from `logs/miloagent.log`

## Questions?

Open an issue with the `question` label — we're happy to help!
