import json
import logging
import re
import signal
import socket
import subprocess
from os import environ as env
from pathlib import Path
from typing import List, Optional
from contextlib import AsyncExitStack
from contextlib import suppress

import a2s
import trio

from utils import ResultNursery

from paho.mqtt import client as paho
from trio_paho_mqtt import AsyncClient

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
        self._nursery: Optional[trio.Nursery] = None

        self._server_started_event = trio.Event()
        self._server_stopping_event = trio.Event()
        self._server_restart_requested = False

        self.log_triggers = {
            r".+: DungeonDB Start": self.handle_server_ready,
            r".+: World saved \( .+": self.handle_world_saved,
            r".+: Got connection SteamID (\d+)": self.handle_player_joined,
            r".+: Closing socket (\d+)": self.handle_player_left,
            r".+: Found location of type (.+)": self.handle_found_location,
            r".+: Time (\d+\.?\d*), day:(\d+)\s+nextm:(\d+\.?\d*)\s+skipspeed:(\d+\.?\d*)": self.handle_new_day,
        }

        self.log_channel: trio.abc.ReceiveChannel
        self._log_channel_tx: trio.abc.SendChannel
        self._log_channel_tx, self.log_channel = trio.open_memory_channel(0)

        self.status_channel: trio.abc.ReceiveChannel
        self._status_channel_tx: trio.abc.SendChannel
        self._status_channel_tx, self.status_channel = trio.open_memory_channel(0)

        self.player_updates_channel: trio.abc.ReceiveChannel
        self._player_updates_channel_tx: trio.abc.SendChannel
        (
            self._player_updates_channel_tx,
            self.player_updates_channel,
        ) = trio.open_memory_channel(0)

        self.location_discovery_channel: trio.abc.ReceiveChannel
        self._location_discovery_channel_tx: trio.abc.SendChannel
        (
            self._location_discovery_channel_tx,
            self.location_discovery_channel,
        ) = trio.open_memory_channel(0)

        self.new_day_channel: trio.abc.ReceiveChannel
        self._new_day_channel_tx: trio.abc.SendChannel
        self._new_day_channel_tx, self.new_day_channel = trio.open_memory_channel(0)

        self._players = set()

    def handle_server_ready(self, match) -> None:
        LOG.info("*** Server started! ***")
        self._server_started_event.set()

    def handle_world_saved(self, match) -> None:
        LOG.info("*** World saved ***")

    def handle_player_joined(self, match) -> None:
        steam_id = match.group(1)
        LOG.info(f"Player joined with Steam ID {steam_id}")
        self._players.add(steam_id)
        self._nursery.start_soon(self.send_player_joined_update, steam_id)

    def handle_player_left(self, match) -> None:
        steam_id = match.group(1)
        LOG.info(f"Player left with Steam ID {steam_id}")
        self._players.remove(steam_id)
        self._nursery.start_soon(self.send_player_left_update, steam_id)

    def handle_found_location(self, match) -> None:
        discovered = match.group(1)
        LOG.info(f"Location of {discovered} discovered")
        self._nursery.start_soon(self.send_location_discovery_update, discovered)

    def handle_new_day(self, match) -> None:
        time, day, nextm, skipspeed = match.groups()
        LOG.info(f"It's a brand new day! ({day})")
        self._nursery.start_soon(self.send_new_day_update, day)

    async def send_player_joined_update(self, steam_id: str) -> None:
        async with self._player_updates_channel_tx.clone() as channel:
            with suppress(trio.WouldBlock):
                channel.send_nowait(dict(action="join", steam_id=steam_id))

    async def send_player_left_update(self, steam_id: str) -> None:
        async with self._player_updates_channel_tx.clone() as channel:
            with suppress(trio.WouldBlock):
                channel.send_nowait(dict(action="leave", steam_id=steam_id))

    async def send_location_discovery_update(self, discovered: str) -> None:
        async with self._location_discovery_channel_tx.clone() as channel:
            with suppress(trio.WouldBlock):
                channel.send_nowait(discovered)

    async def send_new_day_update(self, day: int) -> None:
        async with self._new_day_channel_tx.clone() as channel:
            with suppress(trio.WouldBlock):
                channel.send_nowait(day)

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

        async with AsyncExitStack() as stack:
            # Whenever these memory channels are used, they should be cloned so that we
            # close them properly on exit.
            await stack.enter_async_context(self._log_channel_tx)
            await stack.enter_async_context(self._status_channel_tx)
            await stack.enter_async_context(self._player_updates_channel_tx)
            await stack.enter_async_context(self._location_discovery_channel_tx)
            await stack.enter_async_context(self._new_day_channel_tx)

            restart = True
            while restart:
                self._players = set()
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
                    self._nursery = nursery
                    nursery.start_soon(self.signal_handler)
                    nursery.start_soon(self.log_loop)
                    nursery.start_soon(self.status_loop)
                    rc = await self.proc.wait()
                    LOG.info("Server exited with code %s", rc)
                    nursery.cancel_scope.cancel()
                self._nursery = None
                if rc <= 0 and False:  # TODO: Take out 'and False' when finished testing
                    if self._server_restart_requested:
                        self._server_restart_requested = False
                    else:
                        restart = False
                if restart:
                    LOG.info("\n*** Waiting to restart the server ***\n")
                    self._server_started_event = trio.Event()
                    await trio.sleep(5)  # TODO: Use a back-off

    def shutdown(self):
        LOG.warning("*** Shutting down server ***")
        self._server_stopping_event.set()
        self.proc.send_signal(signal.SIGINT)

    def restart(self):
        LOG.warning("*** Restarting server ***")
        self._server_restart_requested = True
        self.proc.send_signal(signal.SIGINT)

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
                        self.shutdown()
                elif signum == signal.SIGHUP:
                    self.restart()
                elif signum == signal.SIGQUIT:
                    LOG.warning(trio.lowlevel.current_statistics())

    async def log_loop(self) -> None:
        buffer = b""
        async with self._log_channel_tx.clone() as channel:
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
                    with suppress(trio.WouldBlock):
                        channel.send_nowait(line_str)

    async def status_loop(self) -> None:
        status_functions = dict(
            info=a2s.info,
            players=a2s.players,
        )
        await self._server_started_event.wait()
        async with self._status_channel_tx.clone() as channel:
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
                with suppress(trio.WouldBlock):
                    channel.send_nowait(status)
                await trio.sleep(10)


async def publish_log(vhs: ValheimServer, mqtt: AsyncClient) -> None:
    async with vhs.log_channel:
        async for line in vhs.log_channel:
            mqtt.publish("flyte/valheim", line)


async def publish_status(vhs: ValheimServer, mqtt: AsyncClient) -> None:
    async with vhs.status_channel:
        async for status in vhs.status_channel:
            mqtt.publish("flyte/valheim/status", json.dumps(status))


async def publish_players(vhs: ValheimServer, mqtt: AsyncClient) -> None:
    async with vhs.player_updates_channel:
        async for update in vhs.player_updates_channel:
            action = update["action"]
            mqtt.publish(f"flyte/valheim/players/{action}", update["steam_id"])


async def publish_discovery(vhs: ValheimServer, mqtt: AsyncClient) -> None:
    async with vhs.location_discovery_channel:
        async for discovered in vhs.location_discovery_channel:
            mqtt.publish("flyte/valheim/location_discovery", discovered)


async def publish_new_day(vhs: ValheimServer, mqtt: AsyncClient) -> None:
    async with vhs.new_day_channel:
        async for day in vhs.new_day_channel:
            mqtt.publish("flyte/valheim/day", day)


async def async_main() -> None:
    vhs = ValheimServer("Test", "129355", "51773")
    async with trio.open_nursery() as nursery:
        mqtt = AsyncClient(paho.Client(), nursery)
        mqtt.connect("test.mosquitto.org", 1883)

        nursery.start_soon(publish_log, vhs, mqtt)
        nursery.start_soon(publish_status, vhs, mqtt)
        nursery.start_soon(publish_players, vhs, mqtt)
        nursery.start_soon(publish_discovery, vhs, mqtt)
        nursery.start_soon(publish_new_day, vhs, mqtt)
        await vhs.run()
        nursery.cancel_scope.cancel()


def main():
    trio.run(async_main)


if __name__ == "__main__":
    main()
