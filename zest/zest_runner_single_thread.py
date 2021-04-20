"""
Single-threaded runner with abbreviated and verbose display options
"""

import sys
import os
import re
import tempfile
import io
from zest import zest
from zest.zest import log
from zest import zest_finder
from zest.zest_runner_base import ZestRunnerBase, emit_zest_result, open_event_stream
from zest import zest_display
from zest import colors
from zest.zest_display import (
    s,
    set_s_stream,
    display_complete,
    display_timings,
    display_warnings,
    display_start,
    display_stop,
    display_error,
    display_abbreviated,
)


class ZestRunnerSingleThread(ZestRunnerBase):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self.s_stream = None
        if kwargs.get("capture"):
            print("--capture is set and therefore no trace will show up until after run is complete.")
            self.s_stream = tempfile.TemporaryFile(mode="w+")
            set_s_stream(self.s_stream)

        if self.retcode != 0:
            # CHECK that zest_find did not fail
            return

        last_depth = 0
        curr_depth = 0
        event_stream = None

        # Event functions are callbacks from zest
        # ---------------------------------------------------------------------------------
        def event_test_start(zest_result):
            nonlocal last_depth, curr_depth
            if self.verbose >= 2:
                curr_depth = len(zest_result.call_stack) - 1
                display_start(
                    zest_result.short_name, last_depth, curr_depth, self.add_markers
                )
                last_depth = curr_depth

        def event_test_stop(zest_result):
            nonlocal last_depth, curr_depth
            emit_zest_result(zest_result, event_stream)
            self.results += [zest_result]
            curr_depth = len(zest_result.call_stack) - 1
            if self.verbose >= 2:
                display_stop(
                    zest_result.error,
                    zest_result.elapsed,
                    zest_result.skip,
                    last_depth,
                    curr_depth,
                )
            elif self.verbose == 1:
                display_abbreviated(zest_result.error, zest_result.skip)

        # LAUNCH root zests
        for (root_name, (module_name, package, full_path)) in self.root_zests.items():
            with open_event_stream(self.output_folder, root_name) as event_stream:
                root_zest_func = zest_finder.load_module(root_name, module_name, full_path)
                zest.do(
                    root_zest_func,
                    test_start_callback=event_test_start,
                    test_stop_callback=event_test_stop,
                    allow_to_run=self.allow_to_run,
                )

        # COMPLETE
        if self.verbose > 0:
            display_complete(self.root, self.results)

        if self.verbose > 1:
            display_timings(self.results)

        if self.verbose > 0:
            display_warnings(zest._call_warnings)

        self.retcode = 0 if len(zest._call_errors) == 0 else 1

        if self.s_stream is not None:
            self.s_stream.flush()
            self.s_stream.seek(0, io.SEEK_SET)
            captured = self.s_stream.read()
            print(captured)
