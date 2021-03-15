import logging
import re
import signal
import socket
import subprocess
from os import environ as env
from pathlib import Path
from typing import List, Optional

import a2s
import trio

from utils import ResultNursery, Tracer

# from paho.mqtt.client import mqtt
# from trio_paho_mqtt import AsyncClient

THIS_DIR = Path(__file__).parent.absolute()
LD_LIBRARY_PATH = str(THIS_DIR.joinpath("vhserver/linux64"))
IGNORE_OUTPUT = set(("(Filename: ./Runtime/Export/Debug/Debug.bindings.h Line: 35)", ""))

LOG = logging.getLogger(__name__)
LOG.addHandler(logging.StreamHandler())
LOG.setLevel(logging.DEBUG)


class ValheimServer:
    def __init__(self, name: str, world: str, password: Optional[str], port: int = 2456):
        self.name = name
        self.world = world
        self.password = password
        self.port = port
        self.proc: Optional[trio.Process] = None

        self._server_started_event = trio.Event()
        self._server_stopping_event = trio.Event()
        self._server_restart_requested = False

        self.log_triggers = {
            r".+Done generating locations.+": self.handle_locations_generated,
            r".+: World saved \( .+": self.handle_world_saved,
        }

        self.log_channel: trio.abc.ReceiveChannel
        self._log_channel_tx: trio.abc.SendChannel
        self.status_channel: trio.abc.ReceiveChannel
        self._status_channel_tx: trio.abc.SendChannel
        self._log_channel_tx, self.log_channel = trio.open_memory_channel(0)
        self._status_channel_tx, self.status_channel = trio.open_memory_channel(0)

    def handle_locations_generated(self, match) -> None:
        LOG.info("*** Server started! ***")
        self._server_started_event.set()

    def handle_world_saved(self, match) -> None:
        pass

    async def run(self) -> None:
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

        restart = True
        while restart:
            self.proc = await trio.open_process(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=dict(
                    HOME=env["HOME"],
                    LD_LIBRARY_PATH=LD_LIBRARY_PATH,
                    SteamAppId="892970",
                ),
            )
            LOG.info("Starting the server")
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self.signal_handler)
                nursery.start_soon(self.log_loop)
                nursery.start_soon(self.status_loop)
                rc = await self.proc.wait()
                LOG.info("Server exited with code %s", rc)
                nursery.cancel_scope.cancel()
            if rc <= 0 and rc != -15:
                if self._server_restart_requested:
                    self._server_restart_requested = False
                else:
                    restart = False
            if restart:
                LOG.info("\n*** Waiting to restart the server ***\n")
                self._server_started_event = trio.Event()
                await trio.sleep(1)
        await self._log_channel_tx.aclose()
        await self._status_channel_tx.aclose()

    async def signal_handler(self) -> None:
        sigints = 0
        with trio.open_signal_receiver(
            signal.SIGINT, signal.SIGHUP, signal.SIGQUIT
        ) as signal_aiter:
            async for signum in signal_aiter:
                if signum == signal.SIGINT:
                    sigints += 1
                    self._server_stopping_event.set()
                    if self.proc is None:
                        return
                    if sigints >= 3:
                        LOG.warning("*** Killing server ***")
                        self.proc.kill()
                        return
                    if sigints >= 2:
                        LOG.warning("*** Terminating server ***")
                        self.proc.terminate()
                    else:
                        LOG.warning("*** Stopping server ***")
                        self.proc.send_signal(signal.SIGINT)
                elif signum == signal.SIGHUP:
                    LOG.warning("*** Restarting server ***")
                    self._server_restart_requested = True
                    self.proc.send_signal(signal.SIGINT)
                elif signum == signal.SIGQUIT:
                    LOG.warning(trio.lowlevel.current_statistics())

    async def log_loop(self) -> None:
        buffer = b""
        while True:
            rx = await self.proc.stdout.receive_some()
            if not rx:
                return
            buffer += rx
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", maxsplit=1)
                line_str = line.decode("utf8").strip()
                if line_str in IGNORE_OUTPUT:
                    continue
                for pattern, handler in self.log_triggers.items():
                    match = re.match(pattern, line_str)
                    if match:
                        handler(match)
                await self._log_channel_tx.send(line_str)

    async def status_loop(self) -> None:
        status_functions = dict(
            info=a2s.info,
            players=a2s.players,
        )
        await self._server_started_event.wait()
        while not self._server_stopping_event.is_set():
            address = ("localhost", self.port + 1)
            try:
                async with ResultNursery() as nursery:
                    for name, func in status_functions.items():
                        nursery.start_soon_result(
                            name, trio.to_thread.run_sync, func, address
                        )
            except Exception as exc:
                LOG.debug("Exception when getting status from the server: %s", exc)
            status = dict(nursery.results["info"])
            status["players"] = [dict(p) for p in nursery.results["players"]]
            with trio.move_on_after(5):
                await self._status_channel_tx.send(status)
            await trio.sleep(5)


async def async_main() -> None:
    vhs = ValheimServer("Test", "noworld", "testingzzzzzzzz")
    async with trio.open_nursery() as nursery:
        nursery.start_soon(vhs.run)
        # Do MQTT stuff
        async with vhs.log_channel:
            async for line in vhs.log_channel:
                print(line)


def main():
    # trio.run(async_main, instruments=[Tracer()])
    trio.run(async_main)


if __name__ == "__main__":
    main()
