from functools import wraps

import trio


class Colours:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


def set_result(results, key, func):
    @wraps(func)
    async def wrapper(*args, **kwargs):
        results[key] = await func(*args, **kwargs)

    return wrapper


class ResultNursery:
    def __init__(self):
        self.nursery_manager = trio.open_nursery()

    async def __aenter__(self):
        nursery = await self.nursery_manager.__aenter__()
        nursery.results = {}

        def start_soon_result(name, async_fn, *args):
            nursery.start_soon(
                set_result(nursery.results, name, async_fn), *args, name=name
            )

        nursery.start_soon_result = start_soon_result
        return nursery

    async def __aexit__(self, etype, exc, tb):
        await self.nursery_manager.__aexit__(etype, exc, tb)


class Tracer(trio.abc.Instrument):
    def before_run(self):
        print(Colours.OKBLUE + "!!! run started" + Colours.ENDC)

    def _print_with_task(self, msg, task):
        # repr(task) is perhaps more useful than task.name in general,
        # but in context of a tutorial the extra noise is unhelpful.
        print(Colours.OKBLUE + f"{msg}: {task.name}" + Colours.ENDC)

    def task_spawned(self, task):
        self._print_with_task("### new task spawned", task)

    def task_scheduled(self, task):
        self._print_with_task("### task scheduled", task)

    def before_task_step(self, task):
        self._print_with_task(">>> about to run one step of task", task)

    def after_task_step(self, task):
        self._print_with_task("<<< task step finished", task)

    def task_exited(self, task):
        self._print_with_task("### task exited", task)

    def before_io_wait(self, timeout):
        if timeout:
            print(
                Colours.OKBLUE
                + f"### waiting for I/O for up to {timeout} seconds"
                + Colours.ENDC
            )
        else:
            print(Colours.OKBLUE + "### doing a quick check for I/O" + Colours.ENDC)
        self._sleep_time = trio.current_time()

    def after_io_wait(self, timeout):
        duration = trio.current_time() - self._sleep_time
        print(
            Colours.OKBLUE
            + f"### finished I/O check (took {duration} seconds)"
            + Colours.ENDC
        )

    def after_run(self):
        print(Colours.OKBLUE + "!!! run finished" + Colours.ENDC)
