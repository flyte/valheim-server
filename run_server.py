import asyncio
import re
import signal as signals
import sys
from asyncio.streams import StreamReader
from dataclasses import dataclass
from os import environ as env
from pathlib import Path
from typing import Optional, TextIO

import a2s

THIS_DIR = Path(__file__).parent.absolute()
LD_LIBRARY_PATH = str(THIS_DIR.joinpath("vhserver/linux64"))
IGNORE_OUTPUT = set(("(Filename: ./Runtime/Export/Debug/Debug.bindings.h Line: 35)", ""))


@dataclass
class ValheimServerEvent:
    event_type: str


@dataclass
class ServerStartedEvent(ValheimServerEvent):
    pass


class ValheimServer:
    def __init__(self, name: str, world: str, password: Optional[str], port: int = 2456):
        self.name = name
        self.world = world
        self.password = password
        self.port = port
        self.proc: Optional[asyncio.subprocess.Process] = None

        self.loop = asyncio.get_event_loop()
        self.sigints = 0

        self.started = asyncio.Event()
        self.finished = asyncio.Event()

        self.stdout_task: "Optional[asyncio.Task[None]]" = None
        self.stderr_task: "Optional[asyncio.Task[None]]" = None
        self.status_task: "Optional[asyncio.Task[None]]" = None

        self.log_triggers = {
            r".+Done generating locations.+": self.handle_locations_generated
        }

        def sigint_handler() -> None:
            self.stop()
            if self.proc is not None:
                if self.sigints >= 2:
                    self.proc.kill()

        self.loop.add_signal_handler(signals.SIGINT, sigint_handler)

    def handle_locations_generated(self, match):
        self.started.set()

    async def __aenter__(self):
        args = [
            THIS_DIR.joinpath("vhserver/valheim_server.x86_64"),
            "-name",
            self.name,
            "-port",
            str(self.port),
            "-world",
            self.world,
        ]
        if self.password is not None:
            args += ["-password", self.password]

        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(
                HOME=env["HOME"],
                LD_LIBRARY_PATH=LD_LIBRARY_PATH,
                SteamAppId="892970",
            ),
        )
        self.stdout_task = self.loop.create_task(self.print_stdout())
        self.stderr_task = self.loop.create_task(self.print_stderr())
        self.status_task = self.loop.create_task(self.print_status())

        return self

    async def __aexit__(self, exc_type, exc, tb):
        print("*** Stopping the server ***")
        if self.proc is not None:
            self.proc.send_signal(signals.SIGINT)
            exit_code = await self.proc.wait()
            print(f"Server exited with code {exit_code}")

        self.stdout_task.cancel()
        self.stderr_task.cancel()
        self.status_task.cancel()
        await asyncio.gather(
            self.stdout_task, self.stderr_task, self.status_task, return_exceptions=True
        )

    async def print_pipe(self, input: StreamReader, output: TextIO) -> None:
        while self.proc is None:
            await asyncio.sleep(1)
        while True:
            line = (await input.readline()).decode("utf8")
            if not line:
                await asyncio.sleep(1)
                continue
            if line.strip() in IGNORE_OUTPUT:
                continue
            print(line, file=output, end="")
            for pattern, handler in self.log_triggers.items():
                match = re.match(pattern, line)
                if match:
                    handler(match)

    async def print_stdout(self) -> None:
        await self.print_pipe(self.proc.stdout, sys.stdout)

    async def print_stderr(self) -> None:
        await self.print_pipe(self.proc.stderr, sys.stderr)

    async def print_status(self) -> None:
        address = ("localhost", self.port + 1)
        await self.started.wait()
        while True:
            try:
                info, players = await asyncio.gather(
                    a2s.ainfo(address),
                    a2s.aplayers(address),
                )
            except Exception as exc:
                print(f"Exception getting status: {type(exc)}: {exc}")
                await asyncio.sleep(5)
                continue
            print(info)
            print(players)
            await asyncio.sleep(10)


def main():
    async def async_main():
        async with ValheimServer("Test", "noworld", "testingzzzzzzzz") as vhs:
            await vhs.started.wait()

    asyncio.run(async_main())


if __name__ == "__main__":
    main()
