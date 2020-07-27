import threading
import time
import argparse
import ast
import os
import pkgutil
import re
import sys
import traceback
from importlib import import_module, util
import curses

from zest import zest

blue = "\u001b[34m"
yellow = "\u001b[33m"
red = "\u001b[31m"
green = "\u001b[32m"
gray = "\u001b[30;1m"
cyan = "\u001b[36m"
magenta = "\u001b[35m"
bold = "\u001b[1m"
reset = "\u001b[0m"

_tty_size_cache = None


def tty_size():
    global _tty_size_cache
    if _tty_size_cache is None:
        rows, cols = os.popen("stty size", "r").read().split()
        _tty_size_cache = (int(rows), int(cols))
    return _tty_size_cache


log_fp = None
def log(*args):
    global log_fp
    if log_fp is None:
        log_fp = open("log.txt", "w")
    log_fp.write("".join([str(i) for i in args]) + "\n")


class ZestRunner:
    """
    The stand-alone zest runner.

    Searches the cwd for zests/ folders and adds and top-level zest_* function
    that it finds in any module in those folders.

    Expects to the cwd to be the root of your project. This is important
    because the import_module parses the path to inform the package argument.
    """

    n_zest_missing_errors = 0

    tb_pat = re.compile(r"^.*File \"([^\"]+)\", line (\d+), in (.*)")

    def s(self, *strs):
        return sys.stdout.write("".join(strs) + reset)

    def _traceback_match_filename(self, line):
        m = self.tb_pat.match(line)
        if m:
            file = m.group(1)
            lineno = m.group(2)
            context = m.group(3)
            file = os.path.relpath(os.path.realpath(file))

            is_libs = True
            real_path = os.path.realpath(file)
            if real_path.startswith(self.root) and os.path.exists(real_path):
                is_libs = False

            if "/site-packages/" in file:
                # Treat these long but commonly occurring path differently
                file = re.sub(r".*/site-packages/", ".../", file)
            leading, basename = os.path.split(file)
            leading = f"{'./' if len(leading) > 0 and leading[0] != '.' else ''}{leading}"
            return leading, basename, lineno, context, is_libs
        return None

    def display_start(self, name, curr_depth, func):
        """Overload this to change output behavior"""
        if self.last_stack_depth < curr_depth:
            self.s("\n")
        self.last_stack_depth = curr_depth
        marker = "+" if self.add_markers else ""
        self.s("  " * curr_depth, yellow, marker + name, reset, ": ")
        # Note, no \n on this line because it will be added on the display_stop call

    def display_stop(self, name, error, curr_depth, last_depth, elapsed, func):
        """Overload this to change output behavior"""
        if curr_depth < last_depth:
            self.s(f"{'  ' * curr_depth}")

        if isinstance(error, str) and error.startswith("skipped"):
            self.s(bold, yellow, error)
        elif hasattr(func, "skip"):
            self.s(bold, yellow, "SKIPPED ", getattr(func, "skip_reason", "") or "")
        elif error:
            self.s(bold, red, "ERROR", gray, f" (in {int(1000.0 * elapsed)} ms)")
        else:
            self.s(green, "SUCCESS", gray, f" (in {int(1000.0 * elapsed)} ms)")
        self.s("\n")

    def display_abbreviated(self, name, error, func):
        """Overload this to change output behavior"""
        if error:
            self.s(bold, red, "F")
        elif hasattr(func, "skip"):
            skip_code = getattr(func, "skip_code", "s")
            self.s(yellow, skip_code)
        else:
            self.s(green, ".")

    def display_warnings(self, call_warnings):
        for warn in call_warnings:
            self.s(yellow, warn, "\n")

    def error_header(self, edge, edge_style, label):
        return (
            edge_style
            + (edge * 5)
            + " "
            + label
            + " "
            + reset
            + edge_style
            + (edge * (tty_size()[1] - 7 - len(label)))
        )

    def display_error(self, error, stack):
        leaf_test_name = stack[-1]
        formatted_test_name = (
            " . ".join(stack[0:-1]) + bold + " . " + leaf_test_name
        )

        self.s("\n", self.error_header("=", red, formatted_test_name), "\n")
        formatted = traceback.format_exception(
            etype=type(error), value=error, tb=error.__traceback__
        )
        lines = []
        for line in formatted:
            lines += [sub_line for sub_line in line.strip().split("\n")]

        is_libs = False
        for line in lines[1:-1]:
            split_line = self._traceback_match_filename(line)
            if split_line is None:
                self.s(gray if is_libs else "", line, "\n")
            else:
                leading, basename, lineno, context, is_libs = split_line
                if is_libs:
                    self.s(gray, "File ", leading, "/", basename)
                    self.s(gray, ":", lineno)
                    self.s(gray, " in function ")
                    self.s(gray, context, "\n")
                else:
                    self.s("File ", yellow, leading, "/", yellow, bold, basename)
                    self.s(":", yellow, lineno)
                    self.s(" in function ")
                    if leaf_test_name == context:
                        self.s(red, bold, context, "\n")
                    else:
                        self.s(magenta, bold, context, "\n")

        self.s(red, "raised: ", red, bold, error.__class__.__name__, "\n")
        error_message = str(error).strip()
        if error_message != "":
            self.s(red, error_message, "\n")

    def display_errors(self, call_log, call_errors):
        self.s("\n")
        for error, stack in call_errors:
            self.display_error(error, stack)

    def display_complete(self, call_log, call_errors):
        n_errors = len(call_errors)
        self.s(f"\nRan {len(call_log)} tests. ")
        if n_errors == 0:
            self.s(green, "SUCCESS\n")
        else:
            self.s(red, bold, f"{n_errors} ERROR(s)\n")

    def walk(self):
        for folder in self.include_dirs:
            for curr, dirs, files in os.walk(
                os.path.abspath(os.path.join(self.root, folder))
            ):
                dirs[:] = [d for d in dirs if d[0] != "."]
                if curr.endswith("/zests"):
                    yield curr

    def _test_start_callback(self, name, call_stack, func):
        """Track the callback depth and forward to the display_start()"""
        if self.verbose >= 2:
            self.callback_depth = len(call_stack) - 1
            self.display_start(name, self.callback_depth, func)

    def _test_stop_callback(self, name, call_stack, error, elapsed, func):
        """
        Track the callback depth and forward to the
        display_stop() or display_abbreviated()
        """
        self.timings += [(name, elapsed)]
        self.timings += [(name, elapsed)]
        if self.verbose >= 2:
            curr_depth = len(call_stack) - 1
            self.display_stop(
                name, error, curr_depth, self.callback_depth, elapsed, func
            )
            self.callback_depth = curr_depth
        elif self.verbose == 1:
            self.display_abbreviated(name, error, func)

    def _recurse_ast(self, func_body, parent_name, skips, path, lineno):
        """
        Args:
            func_body: the AST func_body or module body
            parent_name: The parent function name or None if a module
            skips: a list of skip names for the current func_body or None if module

        Returns:
            list of children: [(full_name, _groups, skips)]

        """
        n_test_funcs = 0
        found_zest_call = False
        found_zest_call_before_final_func_def = False
        child_list = []
        for i, part in enumerate(func_body):
            if isinstance(part, ast.With):
                child_list += self._recurse_ast(part.body, parent_name, skips, path, part.lineno)

            if isinstance(part, ast.FunctionDef):
                full_name = None
                if parent_name is None:
                    if part.name.startswith("zest_"):
                        full_name = f"{part.name}"
                else:
                    if not part.name.startswith("_"):
                        full_name = f"{parent_name}.{part.name}"

                _groups = []
                _skips = []
                if part.decorator_list:
                    for dec in part.decorator_list:
                        if isinstance(dec, ast.Call):
                            if isinstance(dec.func, ast.Attribute):
                                if dec.func.attr == "group":
                                    if isinstance(dec.args[0], ast.Str):
                                        _groups += [dec.args[0].s]
                                elif dec.func.attr == "skip":
                                    if len(dec.args) > 0 and isinstance(
                                        dec.args[0], ast.Str
                                    ):
                                        _skips += [dec.args[0].s]
                                    elif len(dec.keywords) > 0 and isinstance(
                                        dec.keywords[0].value, ast.Str
                                    ):
                                        _skips += [dec.keywords[0].value.s]

                if full_name is not None:
                    child_list += [(full_name, set(_groups), _skips)]
                    n_test_funcs += 1
                    child_list += self._recurse_ast(part.body, full_name, _skips, path, part.lineno)

                if found_zest_call:
                    found_zest_call_before_final_func_def = True

            if isinstance(part, ast.Expr):
                if isinstance(part.value, ast.Call):
                    if isinstance(part.value.func, ast.Name):
                        if part.value.func.id == "zest":
                            found_zest_call = True

        if (
            n_test_funcs > 0
            and parent_name is not None
            and not found_zest_call
            and not skips
        ):
            ZestRunner.n_zest_missing_errors += 1
            common_wording = "If you are using local functions that are not tests, prefix them with underscore."
            if found_zest_call_before_final_func_def:
                self.s(
                    red, "\nERROR: ",
                    reset,
                    "Zest function '",
                    bold, red,
                    parent_name,
                    reset,
                    f" (@ {path}:{lineno}) ",
                    f"' did not call zest() before all functions were defined. {common_wording}\n",
                )
            else:
                self.s(
                    red, "\nERROR: ",
                    reset,
                    "Zest function '",
                    bold, red,
                    parent_name,
                    reset,
                    f" (@ {path}:{lineno}) ",
                    f"' did not terminate with a call to zest(). {common_wording}\n",
                )

        return child_list

    def _event_considering(self, root_name, module_name, package, member_groups):
        if self.verbose > 2:
            marker = "?" if self.add_markers else ""
            self.s(
                cyan,
                marker + root_name,
                gray,
                f" module_name={module_name}, package={package}, member_groups={member_groups}: ",
            )

    def _event_skip(self, root_name):
        if self.verbose > 2:
            self.s(cyan, f"Skipping\n")

    def _event_running(self, root_name):
        if self.verbose > 2:
            self.s(cyan, f"Running\n")

    def _event_not_running(self, root_name):
        if self.verbose > 2:
            self.s(cyan, f"Not running\n")

    def _event_complete(self):
        self.display_errors(zest._call_log, zest._call_errors)
        self.display_complete(zest._call_log, zest._call_errors)
        if self.verbose > 1:
            self.s("Slowest 5%\n")
            n_timings = len(self.timings)
            self.timings.sort(key=lambda tup: tup[1])
            ninty_percentile = 95 * n_timings // 100
            for i in range(n_timings - 1, ninty_percentile, -1):
                name = self.timings[i]
                self.s("  ", name[0], gray, f" {int(1000.0 * name[1])} ms)\n")

        self.display_warnings(zest._call_warnings)


    def run(
        self,
        root=None,
        verbose=1,
        include_dirs=None,
        match_string=None,
        recurse=0,
        disable_shuffle=False,
        add_markers=False,
    ):
        """
        verbose=0 if you want no output
        verbose=1 if you want normal output (with dots)
        verbose=2 if you want full output (with names)

        groups:
            None runs all
            colon-delimited list to run certain kinds
            "unit" is assumed on all un-grouped tests.
        """
        zest.reset()
        zest._disable_shuffle = disable_shuffle

        self.verbose = verbose
        self.callback_depth = 0
        self.include_dirs = (include_dirs or "").split(":")
        self.timings = []
        self.last_stack_depth = 0
        self.add_markers = add_markers

        # zest runner must start in the root of the project
        # so that modules may be loaded appropriately.
        self.root = root or os.getcwd()
        assert self.root[0] == os.sep
        n_root_parts = len(self.root.split(os.sep))

        allow_to_run = []
        root_zest_funcs = {}
        for curr in self.walk():
            for _, module_name, _ in pkgutil.iter_modules(path=[curr]):
                path = os.path.join(curr, module_name + ".py")
                with open(path) as f:
                    source = f.read()

                module_ast = ast.parse(source)
                zests = self._recurse_ast(module_ast.body, None, None, path, 0)

                for full_name, member_groups, skips in zests:
                    # If the requested substring is anywhere in the full_name
                    # then we add all the parents
                    # eg:
                    #  full_name = "zest_test1.it_does_y.it_does_y1"
                    #  match_string = "it_does_y1"
                    #  Then allow_to_run == [
                    #    "zest_test1"
                    #    "zest_test1.it_does_y"
                    #    "zest_test1.it_does_y.it_does_y1"
                    #  ]

                    parts = full_name.split(".")
                    sub_dirs = curr.split(os.sep)
                    sub_dirs = sub_dirs[n_root_parts:]
                    package = ".".join(sub_dirs)

                    if match_string is None or match_string in full_name:
                        for i in range(len(parts)):
                            allow_to_run += [".".join(parts[0 : i + 1])]

                    if len(parts) == 1:
                        root_zest_funcs[parts[0]] = (
                            module_name,
                            package,
                            member_groups,
                            path,
                        )

        zest._allow_to_run = list(set(allow_to_run))

        has_run = {}

        for (
            root_name,
            (module_name, package, member_groups, full_name),
        ) in root_zest_funcs.items():
            self._event_considering(root_name, module_name, package, member_groups)

            if not has_run.get(root_name):
                self._event_running(root_name)

                spec = util.spec_from_file_location(module_name, full_name)
                mod = util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
                func = getattr(mod, root_name)

                has_run[root_name] = True
                assert len(zest._call_stack) == 0
                zest.do(
                    func,
                    test_start_callback=self._test_start_callback,
                    test_stop_callback=self._test_stop_callback,
                )
            else:
                self._event_not_running(root_name)

        if recurse == 0:
            self._event_complete()
            self.retcode = (
                0
                if len(zest._call_errors) == 0 and ZestRunner.n_zest_missing_errors == 0
                else 1
            )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--verbose",
        default=1,
        type=int,
        help="0=silent, 1=dot-mode, 2=run-trace 3=full-trace",
    )
    parser.add_argument(
        "--include_dirs",
        nargs="?",
        default=os.getcwd(),
        help="Colon-delimited list of directories to search",
    )
    parser.add_argument(
        "--disable_shuffle",
        action="store_true",
        help="Disable the shuffling of test order",
    )
    parser.add_argument(
        "--add_markers", action="store_true", help="Used for internal debugging"
    )
    parser.add_argument(
        "--version", action="store_true", help="Show version and exit",
    )
    parser.add_argument(
        "match_string", type=str, nargs="?", help="Optional substring to match"
    )
    kwargs = vars(parser.parse_args())

    if kwargs.get("version"):
        from . import __version__

        print(__version__)
        sys.exit(0)
    del kwargs["version"]

    runner = ZestRunner().run(**kwargs)
    sys.exit(runner.retcode)


class ZestConsoleUI(ZestRunner):
    """
    Controls console based UI.
    States:
        * main_menu
        * focus: "running/re-running" a test until pass
            - on fail, waits for a file change or 'enter' to re-run
            - on success or "n", moves to next
        * inspection: look at and re-run failed tests
            - 1-9 keys move into focus mode
        * running:
            Shows a status of number of tests run and cumuative errors

    A state implies a set of keyboard commands and a layout
    """

    # Event overloads from super class

    def _event_considering(self, root_name, module_name, package, member_groups):
        pass

    def _event_skip(self, root_name):
        pass

    def _event_running(self, root_name):
        pass

    def _event_not_running(self, root_name):
        pass

    def _event_complete(self):
        self.complete = True


    def _test_start_callback(self, name, call_stack, func):
        """
        This is a callback in the runner thread
        """
        self.dirty = True
        self._current_run_test = name
        time.sleep(0.2)  # Testing delay

    def _test_stop_callback(self, name, call_stack, error, elapsed, func):
        """
        This is a callback in the runner thread
        """
        self._current_run_test = None
        if error is not None:
            self.errors += [(name, error, call_stack)]
            self.n_errors += 1
        else:
            self.n_success += 1

    # Components
    def _draw_title_bar(self):
        # Title bar
        state_menu = self.menu_by_state[self.state]
        rows, cols = self.scr.getmaxyx()
        self.scr.addstr(0, 0, f"{state_menu: <{cols}}", curses.color_pair(1))

    def _draw_summary(self, y):
        n_total = self.n_success + self.n_errors
        if n_total > 0:
            if self.complete:
                self.scr.addstr(y, 0, "Complete:")
            else:
                self.scr.addstr(y, 0, "Running :")
            self.scr.addstr(y, 10, f"{self.n_success} + {self.n_errors} = {n_total}")

    def _draw_fail_lines(self, y):
        if self.n_errors > 0:
            self.scr.addstr(y, 0, f"Failed tests... (select by number to auto-rerun)")
            for i, (test, error, stack) in enumerate(self.errors):
                self.scr.addstr(i+y+1, 0, f"{i+1}) {test}")

    def s(self, *strs):
        self.output_str += "".join(strs) + reset

    def _draw_fail_details(self, y, error):
        self.scr.addstr(y, 0, f"Failed test {error[0]}")

        # TODO: Overloading self.s is pretty hacky. Fix
        # Also printing escape codes inside of curses does not work
        # I'd have to change disply error anyway.
        # See: https://stackoverflow.com/a/35248222
        self.output_str = ""
        self.display_error(self.errors[0][1], self.errors[0][2])
        for i, error_line in enumerate(self.output_str.split("\n")):
            self.scr.addstr(y+i+1, 0, error_line)

    def _draw_awaiting(self, y):
        self.scr.addstr(y, 0, f"Awaiting")

    def _render(self):
        self.scr.clear()
        self._draw_title_bar()
        self.render_funcs[self.state](self)
        self.scr.refresh()
        self.dirty = False

    def _runner_thread(self, *args):
        try:
            self.run(include_dirs="/Users/zack/git/zbs.zest", match_string="zest_basics", verbose=2)
        except KeyboardInterrupt:
            return

    def start_runner_thread(self):
        assert self.runner_thread is None
        self.runner_thread = threading.Thread(target=self._runner_thread, args=())
        self.runner_thread.start()

    def _key_poll_thread(self):
        try:
            self.key = None
            self.key = self.scr.getkey()
            log(f"got key poll {self.key}")
        finally:
            self.key_poll_thread = None

    def start_key_poll_thread(self):
        assert self.key_poll_thread is None
        self.key_poll_thread = threading.Thread(target=self._key_poll_thread, args=())
        self.key_poll_thread.start()

    def __init__(self, scr):
        self.scr = scr
        self.state = "main_menu"
        self.key_poll_thread = None
        self.runner_thread = None
        self.dirty = False
        self.errors = []
        self.n_success = 0
        self.n_errors = 0
        self.complete = False
        self.auto_run_state = "awaiting"
        self.running_state_show_details = None
        self.output_str = ""

        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, curses.COLOR_WHITE)

        while True:
            new_state = self.state_funcs[self.state](self)
            if new_state is None:
                break
            self.state = new_state

    def state_main_menu(self):
        while True:
            self._render()
            key = self.scr.getkey()
            if key == "r":
                return "running"
            # if key == "f":
            #     return self.state_running
            if key == "q":
                return None

    def render_main_menu(self):
        self._draw_summary(y=1)
        self._draw_fail_lines(y=2)

    def state_running(self):
        self.errors = []
        self.n_success = 0
        self.n_errors = 0
        self.complete = False
        self.start_runner_thread()
        self.start_key_poll_thread()
        self.running_state_show_details = None
        while self.runner_thread.is_alive():
            log(f"key1 {self.key}")
            if self.key in [str(i) for i in range(1, 10)]:
                self.running_state_show_details = ord(self.key) - ord("1")
                log(f"self.running_state_show_details {self.running_state_show_details}")
                log(f"errors {self.errors}")
                self.dirty = True
            if self.dirty:
                self._render()
            time.sleep(0.1)
        self.runner_thread = None
        return "main_menu"

    def render_running(self):
        self._draw_summary(y=1)
        self._draw_fail_lines(y=2)
        if self.running_state_show_details is not None:
            self._draw_fail_details(y=3, error=self.errors[self.running_state_show_details])

    def state_auto_run(self):
        self.auto_run_state = "awaiting"
        while True:
            self._render()
            key = self.scr.getkey()
            if key == "m":
                return "main_menu"
            # if key == "r":
            #     return self.state_running
            if key == "q":
                return None

    def render_auto_run(self):
        if self.auto_run_state == "awaiting":
            self._draw_awaiting(y=2)
        if self.auto_run_state == "running":
            if self.n_errors > 0:
                self._draw_fail_details(y=3, error=self.errors[0])

    state_funcs = dict(
        main_menu=state_main_menu,
        running=state_running,
        auto_run=state_auto_run,
    )

    render_funcs = dict(
        main_menu=render_main_menu,
        running=render_running,
        auto_run=render_auto_run,
    )

    menu_by_state = dict(
        main_menu="Main menu:  r)un tests   f)ailed tests   q)uit",
        running="Running:  ^C to stop   1-9 to toggle error details",
        auto_run="Auto-run:  m)ain menu   r)e-run   q)uit",
    )




if __name__ == "__main__":
    # main()
    curses.wrapper(ZestConsoleUI)


"""
Lessons so far:
* There's problems when curses gets recursively called so taht
  when I let the runner call the full zest_runner test suite that
  include calling the zet runner which causes a recursive initialize.
* The ZestRunner has baked-in verbosity settings so I need
  to remove all of those so that the sub class gets a chance to decide
* Thus, I need a clear sepration of concerns about the messages.
  One stage is teh event (which can be trapped by the sub class)
  One stage is the message rendering which might be used by the sub classs
  One stage emits the message which is very different in the subclass

The base class sets up:
    _test_start_callback
    _test_stop_callback

These are handed to the zest.do and cause a callback
But the self.verbose is in that callback:

    def _test_start_callback(self, name, call_stack, func):
        if self.verbose >= 2:
            self.callback_depth = len(call_stack) - 1
            self.display_start(name, self.callback_depth, func)

Which is all sort of wrapped up in the concepts of the base display

Seems like I should just overload those in the sub class
    _test_start_callback
    _test_stop_callback


"""
