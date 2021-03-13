import asyncio
from asyncio.exceptions import CancelledError
from asyncio.tasks import Task
import json
import re
import signal as signals
import sys
from asyncio.streams import StreamReader
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from os import environ as env
from pathlib import Path
from types import TracebackType
from typing import (
    Any,
    AsyncContextManager,
    AsyncGenerator,
    AsyncIterator,
    Awaitable,
    Dict,
    List,
    Optional,
    TextIO,
    Type,
    TypeVar,
)

T = TypeVar("T")

import a2s  # type: ignore
from asyncio_mqtt import Client as MQTTClient  # type: ignore

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

        self.started_event = asyncio.Event()
        self.stopping_event = asyncio.Event()
        self.finished_event = asyncio.Event()

        self.stdout_task: "Optional[asyncio.Task[None]]" = None
        self.stderr_task: "Optional[asyncio.Task[None]]" = None
        self.status_task: "Optional[asyncio.Task[None]]" = None
        self.stop_task: "Optional[asyncio.Task[None]]" = None

        self.log_line_queue: "Optional[asyncio.Queue[str]]" = None
        self.status_queue: "Optional[asyncio.Queue[Dict[str, Any]]]" = None
        self.saved_queue: "Optional[asyncio.Queue[None]]" = None

        self.log_triggers = {
            r".+Done generating locations.+": self.handle_locations_generated,
            r".+: World saved \( .+": self.handle_world_saved,
        }

        def sigint_handler() -> None:
            self.sigints += 1
            if not self.stopping_event.is_set():
                self.stop_task = self.loop.create_task(self.stop())
            if self.proc is not None and self.sigints >= 2:
                print("*** Killing the server ***")
                with suppress(ProcessLookupError):
                    self.proc.kill()
                self.finished_event.set()
            elif self.sigints >= 3:
                print("*** Cancelling all tasks ***")
                for task in asyncio.all_tasks():
                    task.cancel()

        self.loop.add_signal_handler(signals.SIGINT, sigint_handler)

    def handle_locations_generated(self, match: "re.Match[Any]") -> None:
        if not self.stopping_event.is_set():
            self.started_event.set()

    def handle_world_saved(self, match: "re.Match[Any]") -> None:
        if self.saved_queue is not None:
            self.saved_queue.put_nowait(None)

    @asynccontextmanager
    async def log_lines(self) -> AsyncIterator[AsyncGenerator[str, None]]:
        self.log_line_queue = asyncio.Queue()
        yield self._queue_generator(self.log_line_queue)
        self.log_line_queue = None

    @asynccontextmanager
    async def status_updates(self) -> AsyncIterator[AsyncGenerator[Dict[str, Any], None]]:
        self.status_queue = asyncio.Queue()
        yield self._queue_generator(self.status_queue)
        self.status_queue = None

    @asynccontextmanager
    async def saved_updates(self) -> AsyncIterator[AsyncGenerator[None, None]]:
        self.saved_queue = asyncio.Queue()
        yield self._queue_generator(self.saved_queue)
        self.saved_queue = None

    async def _queue_generator(
        self, queue: "asyncio.Queue[T]"
    ) -> AsyncGenerator[T, None]:
        stopping = self.loop.create_task(self.stopping_event.wait())
        while not self.stopping_event.is_set():
            item = self.loop.create_task(queue.get())
            done, _ = await asyncio.wait(
                (stopping, item), return_when=asyncio.FIRST_COMPLETED
            )
            if stopping in done:
                return
            yield await item

    async def __aenter__(self) -> "ValheimServer":
        args: List[str] = [
            str(THIS_DIR.joinpath("vhserver/valheim_server.x86_64")),
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

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> Optional[bool]:
        if not self.stopping_event.is_set():
            await self.stop()
        return None

    async def stop(self) -> None:
        print("*** Stopping the server ***")
        self.stopping_event.set()
        if self.proc is not None:
            with suppress(ProcessLookupError):
                self.proc.send_signal(signals.SIGINT)
            exit_code = await self.proc.wait()
            print(f"Server exited with code {exit_code}")

        self.finished_event.set()

        tasks = [
            t
            for t in (self.stdout_task, self.stderr_task, self.status_task)
            if t is not None
        ]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def print_pipe(self, input: StreamReader, output: TextIO) -> None:
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
        while self.proc is None:
            await asyncio.sleep(1)
        if self.proc.stdout is None:
            return
        await self.print_pipe(self.proc.stdout, sys.stdout)

    async def print_stderr(self) -> None:
        while self.proc is None:
            await asyncio.sleep(1)
        if self.proc.stderr is None:
            return
        await self.print_pipe(self.proc.stderr, sys.stderr)

    async def print_status(self) -> None:
        address = ("localhost", self.port + 1)
        await self.started_event.wait()
        while not self.stopping_event.is_set():
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
            if self.status_queue is not None:
                status = dict(info)
                status["players"] = [dict(p) for p in players]
                self.status_queue.put_nowait(status)
            await asyncio.sleep(10)


async def publish_logs(
    mqtt: MQTTClient, log_line_generator: AsyncGenerator[str, None]
) -> None:
    async for line in log_line_generator:
        await mqtt.publish("flyte/valheim/log", line.strip())


async def publish_status(
    mqtt: MQTTClient, status_generator: AsyncGenerator[Dict[str, Any], None]
) -> None:
    async for status in status_generator:
        await mqtt.publish("flyte/valheim/status", json.dumps(status))


async def publish_saved(
    mqtt: MQTTClient, saved_generator: AsyncGenerator[None, None]
) -> None:
    async for saved in saved_generator:
        await mqtt.publish("flyte/valheim/saved")


async def publish_started(mqtt: MQTTClient, vhs: ValheimServer) -> None:
    await vhs.started_event.wait()
    await mqtt.publish("flyte/valheim/state", "started")


async def publish_stopping(mqtt: MQTTClient, vhs: ValheimServer) -> None:
    await vhs.stopping_event.wait()
    await mqtt.publish("flyte/valheim/state", "stopping")


def main() -> None:
    async def async_main() -> None:
        async with MQTTClient("test.mosquitto.org") as mqtt:
            async with AsyncExitStack() as stack:
                vhs: ValheimServer = await stack.enter_async_context(
                    ValheimServer("Test", "noworld", "testingzzzzzzzz")
                )
                log_lines = await stack.enter_async_context(vhs.log_lines())
                statuses = await stack.enter_async_context(vhs.status_updates())
                loop = asyncio.get_event_loop()
                finished = loop.create_task(vhs.finished_event.wait())
                await mqtt.publish("flyte/valheim/state", "starting")
                coros = [
                    publish_logs(mqtt, log_lines),
                    publish_status(mqtt, statuses),
                    publish_started(mqtt, vhs),
                    publish_stopping(mqtt, vhs),
                ]
                tasks = [loop.create_task(coro) for coro in coros]
                gather_future = asyncio.gather(*tasks)
                try:
                    await asyncio.wait(
                        [finished, gather_future],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    for task in tasks:
                        task.cancel()
                    for task in tasks:
                        with suppress(CancelledError):
                            await task
                    with suppress(CancelledError):
                        await gather_future
            await mqtt.publish("flyte/valheim/state", "stopped")

    asyncio.run(async_main())


if __name__ == "__main__":
    main()
