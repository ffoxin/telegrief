"""Console entry point: telegrief login | run-now | serve."""

from __future__ import annotations

import asyncio

import click

from .config import load_settings


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx: click.Context) -> None:
    """telegrief — ranked queue of Telegram channel posts from your folders."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(serve)


@main.command()
def login() -> None:
    """Interactive one-time login for the user session."""
    from .tg.user import login as tg_login

    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(tg_login(settings.user_session, settings.tg_api_id, settings.tg_api_hash))


@main.command("run-now")
@click.option("--folder", default=None, help="Only this folder (default: all).")
@click.option("--from-h", type=int, default=None, help="Window start, hours ago.")
@click.option("--to-h", type=int, default=None, help="Window end, hours ago.")
def run_now(folder: str | None, from_h: int | None, to_h: int | None) -> None:
    """Run one digest cycle and exit. Do not use while `serve` is running."""
    from .scheduler import run_once

    for line in asyncio.run(run_once(load_settings(), folder, from_h, to_h)):
        click.echo(line)


@main.command()
def serve() -> None:
    """Run forever: daily schedule, read tracking, owner commands."""
    from .scheduler import serve as serve_forever

    asyncio.run(serve_forever(load_settings()))


if __name__ == "__main__":
    main()
