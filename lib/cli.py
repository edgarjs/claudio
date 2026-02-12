"""CLI entry point for Claudio — dispatches subcommands.

Ports the claudio bash script (207 lines) to Python.
Uses sys.argv dispatch (not argparse) to preserve the current CLI UX.
Lazy imports per command for fast startup.
"""

import os
import subprocess
import sys


def _version():
    """Read version from VERSION file."""
    version_file = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "VERSION")
    try:
        with open(version_file) as f:
            return f.read().strip()
    except OSError:
        return "unknown"


def _usage():
    version = _version()
    print(f"""\
Claudio v{version} - Telegram to Claude Code bridge

Usage: claudio <command> [options]

Commands:
  status             Show service and webhook status
  start              Start the HTTP server
  install [bot_name] Install system service + configure a bot (default: "claudio")
  uninstall <bot>    Remove a bot's config (with confirmation)
  uninstall --purge  Stop service, remove all data
  update             Update to the latest release
  restart            Restart the service
  telegram setup     Set up Telegram bot and webhook
  whatsapp setup     Set up WhatsApp Business API webhook
  log [-f] [-n N]    Show logs (-f to follow, -n for line count)
  backup <dest>      Run backup (--hours N, --days N for retention)
  backup status <dest> Show backup status
  backup cron <dest> Install/remove hourly backup cron job
  version            Show version
""")
    sys.exit(1)


def _parse_retention_args(args):
    """Parse --hours and --days flags from args list.

    Returns (hours, days, remaining_args).
    """
    hours = 24
    days = 7
    i = 0
    while i < len(args):
        if args[i] == "--hours":
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                print("Error: --hours requires a positive integer.", file=sys.stderr)
                sys.exit(1)
            hours = int(args[i + 1])
            i += 2
        elif args[i] == "--days":
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                print("Error: --days requires a positive integer.", file=sys.stderr)
                sys.exit(1)
            days = int(args[i + 1])
            i += 2
        else:
            print(f"Error: Unknown argument '{args[i]}'.", file=sys.stderr)
            sys.exit(1)
    return hours, days


def _handle_log(config, args):
    """Handle 'claudio log' command."""
    follow = False
    lines = 50
    i = 0
    while i < len(args):
        if args[i] in ("-f", "--follow"):
            follow = True
            i += 1
        elif args[i] in ("-n", "--lines"):
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                print(
                    "Error: -n/--lines requires a positive integer argument.",
                    file=sys.stderr)
                sys.exit(1)
            lines = int(args[i + 1])
            i += 2
        else:
            print(
                f"Error: Unknown argument '{args[i]}'. "
                "Usage: claudio log [-f|--follow] [-n|--lines N]",
                file=sys.stderr)
            sys.exit(1)

    log_file = config.log_file
    if not os.path.isfile(log_file):
        print(f"No log file found at {log_file}")
        sys.exit(1)

    tail_args = ["tail", "-n", str(lines)]
    if follow:
        tail_args.append("-f")
    tail_args.append(log_file)
    os.execvp("tail", tail_args)


def _handle_backup(args):
    """Handle 'claudio backup' subcommands."""
    from lib.backup import (
        backup_run, backup_status, backup_cron_install, backup_cron_uninstall)

    if not args or args[0] in ("", "--help", "-h"):
        print("Usage: claudio backup <destination> [--hours N] [--days N]")
        print("       claudio backup status <destination>")
        print("       claudio backup cron install <destination> [--hours N] [--days N]")
        print("       claudio backup cron uninstall")
        sys.exit(0)

    subcmd = args[0]

    if subcmd == "status":
        if len(args) < 2:
            print("Usage: claudio backup status <destination>", file=sys.stderr)
            sys.exit(1)
        sys.exit(backup_status(args[1]))

    if subcmd == "cron":
        cron_args = args[1:]
        cron_action = cron_args[0] if cron_args else "install"

        if cron_action == "install":
            if len(cron_args) < 2:
                print(
                    "Usage: claudio backup cron install <destination> "
                    "[--hours N] [--days N]", file=sys.stderr)
                sys.exit(1)
            dest = cron_args[1]
            hours, days = _parse_retention_args(cron_args[2:])
            sys.exit(backup_cron_install(dest, hours, days))
        elif cron_action in ("uninstall", "remove"):
            sys.exit(backup_cron_uninstall())
        else:
            print(
                "Usage: claudio backup cron "
                "{install <dest> [--hours N] [--days N]|uninstall}",
                file=sys.stderr)
            sys.exit(1)

    # Direct backup: claudio backup <dest> [--hours N] [--days N]
    dest = subcmd
    hours, days = _parse_retention_args(args[1:])
    sys.exit(backup_run(dest, hours, days))


def main():
    """Main CLI entry point."""
    from lib.config import ClaudioConfig

    config = ClaudioConfig()
    config.init()

    args = sys.argv[1:]
    cmd = args[0] if args else ""

    if cmd in ("version", "--version", "-v"):
        print(f"claudio v{_version()}")
        sys.exit(0)

    if cmd == "status":
        from lib.service import service_status
        service_status(config)

    elif cmd == "start":
        from lib.service import server_start
        server_start(config)

    elif cmd == "install":
        from lib.service import service_install
        bot_id = args[1] if len(args) > 1 else "claudio"
        service_install(config, bot_id)

    elif cmd == "uninstall":
        from lib.service import service_uninstall
        service_uninstall(config, args[1] if len(args) > 1 else "")

    elif cmd == "update":
        from lib.service import service_update
        service_update(config)

    elif cmd == "restart":
        from lib.service import service_restart
        service_restart()

    elif cmd == "log":
        _handle_log(config, args[1:])

    elif cmd == "backup":
        _handle_backup(args[1:])

    elif cmd == "telegram":
        if len(args) > 1 and args[1] == "setup":
            from lib.setup import telegram_setup
            telegram_setup(config)
        else:
            print("Usage: claudio telegram setup")
            sys.exit(1)

    elif cmd == "whatsapp":
        if len(args) > 1 and args[1] == "setup":
            from lib.setup import whatsapp_setup
            whatsapp_setup(config)
        else:
            print("Usage: claudio whatsapp setup")
            sys.exit(1)

    else:
        _usage()
