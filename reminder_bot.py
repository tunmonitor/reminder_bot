#!/usr/bin/env python3
"""Root-level entrypoint for the reminder bot.

This wrapper keeps compatibility with the project specification and can be used
as a direct launch script in real deployments.
"""

from bot.reminder_bot import main


if __name__ == "__main__":
    raise SystemExit(main())
