# Contributing to Claudio

Contributions are welcome! Bug reports, feature requests, documentation fixes, and code contributions are all appreciated.

## Getting Started

1. Fork the repository on GitHub
2. Clone your fork locally:
   ```bash
   git clone https://github.com/YOUR_USERNAME/claudio.git
   cd claudio
   ```
3. Create a branch for your work:
   ```bash
   git checkout -b feature/your-feature-name
   ```

## Development Setup

Claudio is a shell/Python project with no build system or linter.

Run locally with:

```bash
./claudio start
```

Runtime configuration and state are stored in `$HOME/.claudio/` (not in the repo).

## Project Structure

- `claudio` — Main CLI entry point, dispatches subcommands
- `lib/config.sh` — Config loading and env file management
- `lib/server.sh` — Starts the Python HTTP server and cloudflared tunnel
- `lib/server.py` — Python HTTP server (stdlib `http.server`, port 8421)
- `lib/telegram.sh` — Telegram Bot API integration
- `lib/claude.sh` — Claude Code CLI wrapper with conversation context
- `lib/history.sh` — Conversation history management
- `lib/service.sh` — systemd/launchd service management and cloudflared setup

## Running Tests

Claudio uses [BATS](https://github.com/bats-core/bats-core) for testing.

```bash
# Install BATS (macOS)
brew install bats-core

# Run all tests
bats tests/

# Run a specific test file
bats tests/db.bats
```

Tests are located in the `tests/` directory. When contributing, please:

- Run existing tests before submitting changes
- Add tests for new functionality when possible
- Ensure all tests pass

## Git Hooks

The project includes a pre-commit hook that runs tests before each commit. To enable it:

```bash
git config core.hooksPath .githooks
```

This ensures tests pass before any commit is allowed.

## Making Changes

- Check existing issues and PRs to avoid duplicate work
- For larger changes, open an issue first to discuss your approach
- Follow existing code style and conventions
- Test your changes locally before submitting
- Keep commits focused and atomic

## Submitting a Pull Request

1. Push your branch to your fork
2. Open a pull request with a clear description of what and why
3. Reference related issues if applicable (e.g., "Fixes #123")

## Security

Claudio runs Claude Code with `--dangerously-skip-permissions`. When contributing:

- Be mindful of security implications in any change
- Do not introduce vulnerabilities that could expose user systems
- Report security issues privately to maintainers rather than opening a public issue

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
