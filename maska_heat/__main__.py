"""Entry point: `python -m maska_heat` runs the app; `python -m maska_heat notify-test` sends a test notification;
`python -m maska_heat fix-hot-water [--apply]` corrects hot water readings logged as heating outside the schedule."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import ConfigError, load_secrets
from .settings import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maska_heat")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "notify-test", "fix-hot-water"])
    parser.add_argument("--config", type=Path, help="path to config.yaml")
    parser.add_argument("--secrets", type=Path, help="path to secrets.yaml")
    parser.add_argument("--since", default="2026-11-09", help="fix-hot-water: first local day (YYYY-MM-DD)")
    parser.add_argument("--apply", action="store_true", help="fix-hot-water: write the correction (default: only list)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings(args.config)
        secrets = load_secrets(args.secrets)
    except (ConfigError, ValueError) as e:
        print(f"config error: {e}", file=sys.stderr)
        return 1

    if args.command == "notify-test":
        from .db import Database
        from .notify import Notifier

        async def send() -> bool:
            db = Database(settings.database_path)
            n = Notifier(settings.notify, (secrets.get("ntfy") or {}).get("topic"), db, ZoneInfo(settings.location.timezone))
            try:
                if not n.available:
                    print("ntfy topic missing in secrets.yaml (ntfy: topic: ...)", file=sys.stderr)
                    return False
                return await n.send("test", "maska_heat test", "Notifications work 👍", priority="high", tags="fire")
            finally:
                await n.aclose()
                db.close()

        return 0 if asyncio.run(send()) else 1

    if args.command == "fix-hot-water":
        import time
        from datetime import datetime

        from .db import Database
        from .repair import fix_hot_water

        tz = ZoneInfo(settings.location.timezone)
        try:
            since = datetime.fromisoformat(args.since).replace(tzinfo=tz)
        except ValueError:
            print(f"--since: not a date: {args.since}", file=sys.stderr)
            return 1
        db = Database(settings.database_path)
        try:
            report = fix_hot_water(settings, db, int(since.timestamp()), int(time.time()), apply=args.apply)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        finally:
            db.close()
        print(report.text(tz))
        return 0

    import uvicorn

    from .web.app import create_app

    uvicorn.run(create_app(settings, secrets), host=settings.web.host, port=settings.web.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
