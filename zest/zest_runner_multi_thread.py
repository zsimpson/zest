import time
import json
import os
import re
import io
import random
import sys
import signal
import multiprocessing
import traceback
import pathlib
from multiprocessing import Queue
from queue import Empty
from collections import deque
from pathlib import Path
from zest import zest
from zest.zest import ZestResult
from zest.zest_runner_base import ZestRunnerBase
from zest import zest_finder
from zest.zest import log
from subprocess import Popen, DEVNULL
from dataclasses import dataclass
from contextlib import redirect_stdout, redirect_stderr
from zest.zest_display import (
    s,
    display_complete,
    display_timings,
    display_warnings,
    display_start,
    display_stop,
    display_error,
)


def read_zest_result_line(fd):
    while True:
        line = fd.readline()
        if not line:
            break

        if not isinstance(line, str):
            line = line.decode()

        yield ZestResult.loads(line)


def _do_work_order(
    root_name,
    module_name,
    full_path,
    output_folder,
    capture_stdio,
    allow_to_run,
    disable_shuffle,
    bypass_skip,
):
    zest.reset(disable_shuffle, bypass_skip)

    event_stream = open(f"{output_folder}/{root_name}.evt", "wb", buffering=0)

    # It may be very slow to have the load_module here in the child
    # processes as it means that each child will have to load_module
    # and get no benefit from caching of modules. It might be better
    # to move this in to the parent process
    root_zest_func = zest_finder.load_module(root_name, module_name, full_path)

    zest_result_to_return = None

    try:

        def emit_zest_result(zest_result, stream):
            assert isinstance(zest_result, ZestResult)
            try:
                msg = (zest_result.dumps() + "\n").encode()
                stream.write(msg)
                stream.flush()
            except TypeError:
                log(f"Serialization error on {zest_result}")

        def event_callback(zest_result):
            """
            This callback occurs anytime a sub-zest starts or stops.
            """
            emit_zest_result(zest_result, event_stream)
            _do_work_order.queue.put(zest_result)
            nonlocal zest_result_to_return
            zest_result_to_return = zest_result

        zest._capture_stdio = capture_stdio
        zest.do(
            root_zest_func,
            test_start_callback=event_callback,
            test_stop_callback=event_callback,
            allow_to_run=allow_to_run,
        )
    except Exception as e:
        e._formatted = traceback.format_exception(
            etype=type(e), value=e, tb=e.__traceback__
        )
        _do_work_order.queue.put(e)

    finally:
        event_stream.close()

    return zest_result_to_return


def _do_worker_init(queue):
    _do_work_order.queue = queue


class ZestRunnerMultiThread(ZestRunnerBase):
    state_messages = ["DONE", "RUNNING"]

    def n_live_procs(self):
        return len([proc for proc in self.procs if proc.exit_code is None])

    def poll(self, request_stop):
        """
        Check the status of all running threads
        Returns:
            True if there's more to do
            False if everything is done

        Usage:
            def callback(zest_result):
                ...

            runner = ZestRunnerMultiThread(callback=callback, ...)
            while runner.poll(request_stop):
                if ...: request_stop = True
        """

        if request_stop and self.pool is not None:
            self.pool.terminate()
            # for proc in self.procs:
            #     if proc.exit_code is not None:
            #         try:
            #             os.kill(proc.child_pid, signal.SIGKILL)
            #         except ProcessLookupError:
            #             log(f"KILL failed {proc.child_pid}")

        try:
            while True:
                zest_result = self.queue.get_nowait()
                if isinstance(zest_result, Exception):
                    raise zest_result
                assert isinstance(zest_result, ZestResult)
                worker_i = self.pid_to_worker_i.get(zest_result.pid)
                if worker_i is None:
                    self.pid_to_worker_i[zest_result.pid] = len(self.pid_to_worker_i)
                zest_result.worker_i = self.pid_to_worker_i[zest_result.pid]
                self.worker_status[zest_result.worker_i] = zest_result
                if not zest_result.is_running:
                    self.results += [zest_result]

                if self.callback is not None:
                    self.callback(zest_result)
        except Empty:
            pass

        if (
            self.map_results is not None
            and self.map_results.ready()
            and self.queue.empty()
        ):
            self.pool.join()
            return False

        return True

    def draw_status(self):
        """
        Draw worker status one line per worker with a Clear to EOL.
        If run us complete, then clear all those lines
        Return the cursor to the start line
        """

        def cursor_move_up(n_lines):
            sys.stdout.write(f"\033[{n_lines}A")

        def cursor_clear_to_eol_and_newline():
            sys.stdout.write("\033[K\n")

        def write_line(line):
            if len(line) > 0:
                assert line[-1] != "\n"
                sys.stdout.write(line)
            cursor_clear_to_eol_and_newline()

        for i, worker in enumerate(self.worker_status):
            if self.run_complete:
                write_line("")
            else:
                if worker is not None:
                    write_line(
                        f"{i:2d}: {self.state_messages[worker.is_running]:<8s} {worker.full_name}"
                    )
                else:
                    write_line(f"{i:2d}: NOT STARTED")

        cursor_move_up(len(self.worker_status))

    def draw_complete(self):
        display_complete("", self.results)

        if self.verbose > 1:
            # When verbose then AFTER the multithreads have all had a chance
            # to run THEN we can dump the run logs.
            # This is particularly important for the advanced tests so that
            # they can see what ran.
            for result in self.results:
                display_start(result.full_name, None, None, self.add_markers)
                display_stop(result.error, result.elapsed, result.skip, None, None)

    def __init__(self, n_workers=2, allow_output=True, **kwargs):
        super().__init__(**kwargs)

        self.n_workers = n_workers
        self.pid_to_worker_i = {}
        self.worker_status = [None] * self.n_workers
        self.pool = None
        self.queue = Queue()
        self.map_results = None
        self.allow_output = allow_output
        self.run_complete = False

    def message_pump(self):
        if self.retcode != 0:
            # CHECK that zest_find did not fail
            return self

        request_stop = False
        while True:
            try:
                # if ...: request_stop = True
                #   TODO

                if self.allow_output:
                    self.draw_status()

                if not self.poll(request_stop):
                    self.run_complete = True
                    break

            except KeyboardInterrupt:
                request_stop = True
                self.retcode = 1

        if self.allow_output:
            self.draw_status()
            self.draw_complete()

    def run(self):
        if self.retcode != 0:
            # CHECK that zest_find did not fail
            return self

        work_orders = []
        for (root_name, (module_name, package, full_path),) in self.root_zests.items():
            work_orders += [
                (
                    root_name,
                    module_name,
                    full_path,
                    self.output_folder,
                    self.capture_stdio,
                    self.allow_to_run,
                    self.disable_shuffle,
                    self.bypass_skip,
                )
            ]

            result_filename = self.output_folder / f"{root_name}.evt"
            try:
                os.remove(result_filename)
            except:
                pass

        # multiprocessing.Queue can only be passed via the pool initializer, not as an arg.
        self.pool = multiprocessing.Pool(self.n_workers, _do_worker_init, [self.queue])
        self.map_results = self.pool.starmap_async(_do_work_order, work_orders)
        self.pool.close()
        # self.message_pump()

        # with multiprocessing.Pool(
        #     self.n_workers, _do_worker_init, [self.queue]
        # ) as self.pool:
        #     self.map_results = self.pool.starmap_async(_do_work_order, work_orders)
        #     self.pool.close()
        #     self.message_pump()

        return self
