import asyncio
import re
import signal as signals
import sys
from asyncio.streams import StreamReader
from contextlib import asynccontextmanager
from dataclasses import dataclass
from os import environ as env
from pathlib import Path
from typing import AsyncGenerator, AsyncIterator, Optional, TextIO

import a2s
from asyncio_mqtt import Client as MQTTClient

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
        self.stopping = asyncio.Event()
        self.finished = asyncio.Event()

        self.stdout_task: "Optional[asyncio.Task[None]]" = None
        self.stderr_task: "Optional[asyncio.Task[None]]" = None
        self.status_task: "Optional[asyncio.Task[None]]" = None
        self.stop_task: "Optional[asyncio.Task[None]]" = None

        self.log_line_queue: "Optional[asyncio.Queue[str]]" = None

        self.log_triggers = {
            r".+Done generating locations.+": self.handle_locations_generated
        }

        def sigint_handler() -> None:
            self.sigints += 1
            if not self.stopping.is_set():
                self.stop_task = self.loop.create_task(self.stop())
            if self.proc is not None and self.sigints >= 2:
                print("***Killing Server***")
                self.proc.kill()
                self.finished.set()

        self.loop.add_signal_handler(signals.SIGINT, sigint_handler)

    def handle_locations_generated(self, match):
        self.started.set()

    @asynccontextmanager
    async def log_lines(self) -> AsyncIterator[AsyncGenerator[str, None]]:
        self.log_line_queue = asyncio.Queue()

        async def log_generator() -> AsyncGenerator[str, None]:
            finished = self.loop.create_task(self.finished.wait())
            while not self.finished.is_set():
                log_line = self.loop.create_task(self.log_line_queue.get())
                done, _ = await asyncio.wait(
                    (finished, log_line), return_when=asyncio.FIRST_COMPLETED
                )
                if finished in done:
                    return
                yield await log_line

        yield log_generator
        self.log_line_queue = None

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

        return self.log_lines()

    async def __aexit__(self, exc_type, exc, tb):
        if not self.stopping.is_set():
            await self.stop()

    async def stop(self):
        print("*** Stopping the server ***")
        self.stopping.set()
        if self.proc is not None and self.proc.returncode is None:
            # Maybe just try catching the case in which the process doesn't exist,
            # because it could quit at any moment, even though returncode is not None.
            self.proc.send_signal(signals.SIGINT)
            exit_code = await self.proc.wait()
            print(f"Server exited with code {exit_code}")

        self.finished.set()

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
                # EOF so the pipe has been closed
                return
            if line.strip() in IGNORE_OUTPUT:
                continue
            print(line, file=output, end="")
            if self.log_line_queue is not None:
                self.log_line_queue.put_nowait(line)
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
        while not self.stopping.is_set():
            try:
                info, players = await asyncio.gather(
                    a2s.ainfo(address),
                    a2s.aplayers(address),
                )
            except asyncio.CancelledError:
                raise
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
            async with vhs as log_lines:
                async for line in log_lines():
                    print("oooo")

    asyncio.run(async_main())


if __name__ == "__main__":
    main()
