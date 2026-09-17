"""
Minimal logging context for Oatgrass.
Single place to control all output: screen + file, with flush.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime
from typing import Callable, Optional, Union
from rich.console import Console, Group
from rich.live import Live
from rich.text import Text
from oatgrass.__version__ import __version__

class OatgrassLogger:
    """Minimal logger: print to screen + file, always flush"""
    
    def __init__(
        self,
        log_file: Optional[Path] = None,
        debug: bool = False,
        show_pacing_status: bool = True,
        quiet: bool = False,
    ):
        self.log_file = log_file
        self._file_handle = None
        self._console = Console()
        self._start_time = datetime.now()
        self.debug_mode = debug
        self.show_pacing_status = show_pacing_status
        self._rate_limit_note_trackers: set[str] = set()
        self._throttle_note_trackers: set[str] = set()
        self._status_active = False
        self._status_len = 0
        self._status_context: Optional[Union[str, Callable[[], str]]] = None
        self._live: Optional[Live] = None
        self._live_task_text = ""
        self._live_wait_text: Optional[str] = None

        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            self._file_handle = open(log_file, 'w', buffering=1, encoding='utf-8')  # Line buffered, UTF-8

        # quiet=True is for get_logger()'s auto-created fallback instance only:
        # that one exists because *something* called a logging function without
        # any real session ever being set up (e.g. an atexit hook's own debug
        # logging, running after the real session already printed its own
        # goodbye and started exiting) -- it must stay silent, not announce a
        # session nobody asked for, possibly after the real one already ended.
        if not quiet:
            welcome = f"({self._start_time.strftime('%H:%M:%S')}  Started Oatgrass {__version__})"
            self.progress(welcome)

    def _clear_status_line(self) -> None:
        if not self._status_active:
            return
        clear = "\r" + (" " * self._status_len) + "\r"
        print(clear, end="", flush=True)
        sys.stdout.flush()
        self._status_active = False
        self._status_len = 0

    @contextmanager
    def status_context(self, text: Union[str, Callable[[], str]]):
        """While active, any other status() call is shown alongside `text`
        instead of clobbering it -- e.g. a pacing wait that fires mid-page
        during a collage fetch still shows "[Page N of M] ... | [pacing] ..."
        instead of losing the page/ETA context the pacing line knows nothing
        about.

        Pass a callable, not a plain string, when the context has a live
        "elapsed" figure that should keep ticking while it's on screen (e.g.
        a multi-second pacing wait) -- a static string is computed once and
        would otherwise freeze at whatever elapsed time it happened to be
        when the wait started, even though real time keeps passing during
        the wait itself."""
        previous = self._status_context
        self._status_context = text
        try:
            yield
        finally:
            self._status_context = previous

    def _current_context_text(self) -> Optional[str]:
        context = self._status_context
        if context is None:
            return None
        return context() if callable(context) else context

    @contextmanager
    def live_progress(self):
        """Open a transient, Rich-managed two-line progress region: a task
        line, and an optional "waiting" line braced under it while a pacing
        wait is in progress. Unlike status()'s single "\\r"-overwrite row,
        Live tracks how many physical rows it actually rendered (correctly
        accounting for real terminal width, including any wraps) and moves
        the cursor up by exactly that many before redrawing or erasing --
        the primitive the old single-row assumption was missing.

        Any log()/warning()/etc. call made while this is open is printed by
        Rich automatically above the live region (same Console instance,
        which Live coordinates natively) and the region redraws cleanly
        below it. On exit, the region is fully erased -- no frozen last
        frame left behind."""
        self._clear_status_line()
        self._live_task_text = ""
        self._live_wait_text = None
        live = Live(console=self._console, transient=True, auto_refresh=False)
        self._live = live
        live.start()
        try:
            yield self
        finally:
            self._live = None
            live.stop()

    def set_live_task(self, text: Union[str, Callable[[], str]]) -> None:
        """Update the outer (container) line of the live progress region.
        Starting a new task tick also clears any leftover wait line from a
        previous page's pacing wait -- a fresh task means no wait is in
        progress until/unless a new one begins.

        Pass a callable, not a plain string, when the task line has a live
        "elapsed" figure that should keep ticking for as long as a wait line
        is braced under it -- set_live_wait() re-renders this same task text
        on every wait tick, so a frozen string would freeze elapsed again for
        the whole wait, the exact bug this replaced status_context() to fix."""
        self._live_task_text = text
        self._live_wait_text = None
        self._render_live()

    def set_live_wait(self, text: Optional[str]) -> None:
        """Update (or clear, with None) the inner "waiting" line braced
        under the task line."""
        self._live_wait_text = text
        self._render_live()

    def _render_live(self) -> None:
        if self._live is None:
            return
        task_text = self._live_task_text() if callable(self._live_task_text) else self._live_task_text
        task = Text(task_text)
        task.stylize("cyan", 0, self._leading_bracket_end(task_text))
        lines = [task]
        if self._live_wait_text:
            wait = Text(f"└ {self._live_wait_text}")
            wait.stylize("grey50", 0, len("└ ") + self._leading_bracket_end(self._live_wait_text))
            lines.append(wait)
        # refresh=True: auto_refresh is off (we redraw exactly on our own
        # set_live_task()/set_live_wait() calls, not on a timer), so without
        # this update() stores the renderable but never actually draws it.
        self._live.update(Group(*lines), refresh=True)

    @staticmethod
    def _leading_bracket_end(text: str) -> int:
        """Length of a leading "[...]" tag, or the whole string if there
        isn't one. Locating a tag's own boundary within a message whose
        severity is already known (progress() was explicitly called) is a
        different thing from guessing severity from content -- it's just
        "where does the label end," the same job the fixed-length literal
        prefixes (`"[WARNING] "`, 9 chars) already do for info/warning/error/
        success/result, generalized for progress's variable-length tags
        ("[Task 7 of 987]" vs "[paced]") instead of a hardcoded prefix."""
        if text.startswith("[") and (close := text.find("]")) != -1:
            return close + 1
        return len(text)

    def status(self, msg: str) -> str:
        """Update single-line status in-place on screen only. Returns the
        text actually rendered, after merging any active status_context.

        While a live_progress() region is open, this instead updates its
        "waiting" line -- callers (e.g. rate_limits.py's pacing wait) don't
        need to know or care which display mechanism is active; they just
        report a status and get the right rendering either way.

        Styled the same "progress" tag-only cyan as [Task N of M]/[paced]/etc
        (and the same bracket-only convention info/warning/error/success/
        result already use) -- status() is just the in-place-overwrite
        sibling of progress(), not a separate rendering path with its own
        rules.

        When a status_context is merged in, the context's tag is dimmed
        relative to the fresh message's tag: the context is typically an
        estimate (elapsed/remaining, itself frozen from before whatever is
        merging in), the fresh message is typically a fact (an exact pacing
        countdown) -- they shouldn't compete for the reader's eye with equal
        visual weight."""
        if self._live is not None:
            self.set_live_wait(msg)
            return msg
        context_text = self._current_context_text()
        merged = context_text is not None and msg != context_text
        combined = f"{context_text} | {msg}" if merged else msg
        rendered = combined.rstrip("\n")
        padded_len = max(len(rendered), self._status_len)
        padded = rendered + (" " * (padded_len - len(rendered)))
        text = Text(padded)
        if merged:
            text.stylize("grey50", 0, self._leading_bracket_end(context_text))
            fresh_start = len(context_text) + len(" | ")
            text.stylize("cyan", fresh_start, fresh_start + self._leading_bracket_end(msg))
        else:
            text.stylize("cyan", 0, self._leading_bracket_end(msg))
        print("\r", end="", flush=True)
        # soft_wrap=True: this must stay one physical terminal line, no
        # matter how long -- Rich's default hard-wrap-to-width would insert
        # a real newline mid-status, and the leading "\r" overwrite trick
        # only returns to the start of the *current* row, so a wrapped
        # remainder would be left behind as visual debris on every redraw.
        self._console.print(text, end="", soft_wrap=True)
        sys.stdout.flush()
        self._status_active = True
        self._status_len = padded_len
        return rendered

    def clear_status(self) -> None:
        """Clear in-place status line, if any (or the live wait line, if a
        live_progress() region is open)."""
        if self._live is not None:
            self.set_live_wait(None)
            return
        self._clear_status_line()

    def _screen_text(self, output: str) -> Text:
        text = Text(output)
        for prefix, style, end in (
            ("[INFO] ", "cyan", 6),
            ("[WARNING] ", "yellow", 9),
            ("[ERROR] ", "red", 7),
            ("[OK] ", "green", 4),
            # Match-verdict labels are deliberately their own vocabulary, not
            # error/warning/success: "the target already has this" and "found
            # an upload candidate" aren't statements about whether the run is
            # healthy, they're the actual domain conclusion the user scans
            # for -- conflating them with generic severity is what made a
            # genuinely good find (a candidate) look the same shade of
            # "warning" as an ambiguous case or an actual failure.
            ("[Result: candidate] ", "bold green", len("[Result: candidate]")),
            ("[Result: possible candidate] ", "yellow", len("[Result: possible candidate]")),
            ("[Result: duplicate] ", "grey50", len("[Result: duplicate]")),
        ):
            if output.startswith(prefix):
                text.stylize(style, 0, end)
                return text
        if output.startswith("   Candidate found: "):
            text.stylize("yellow")
        elif output.startswith("   Match found on target. Not a candidate."):
            text.stylize("red")
        elif output.startswith("   ") and " group #" in output and " torrent #" in output and "'" in output:
            if (quote_start := output.find("'")) > 0 and (quote_end := output.rfind("'")) > quote_start:
                text.stylize("grey50", 0, quote_start)
                text.stylize("yellow", quote_start, quote_end + 1)
        return text

    @staticmethod
    def _apply_indent(output: str, indent: int) -> str:
        if indent <= 0:
            return output
        if output == "":
            return ""
        padding = " " * indent
        return "\n".join(f"{padding}{line}" if line else "" for line in output.split("\n"))
    
    def log(self, msg: str, prefix: str = "", indent: int = 0, style: str | None = None):
        """Log to screen and file. `style` bypasses _screen_text()'s
        prefix-matching entirely for callers (currently only progress())
        that already know their own severity and must not be guessed at
        from message content. Styles only the leading "[...]" tag, matching
        the bracket-only convention every other severity already uses --
        the body stays natural text, not the whole line."""
        output = f"{prefix}{msg}" if prefix else msg
        output = self._apply_indent(output, max(0, indent))
        self._clear_status_line()

        # Screen (unbuffered)
        if style is not None:
            text = Text(output)
            text.stylize(style, 0, self._leading_bracket_end(output))
        else:
            text = self._screen_text(output)
        self._console.print(text)

        # File
        if self._file_handle:
            self._file_handle.write(output + "\n")
            self._file_handle.flush()  # Force flush
            import os
            os.fsync(self._file_handle.fileno())  # Force OS write
    
    def info(self, msg: str, indent: int = 0):
        """Info message"""
        self.log(msg, indent=indent)
    
    def warning(self, msg: str, indent: int = 0):
        """Warning message"""
        self.log(msg, "[WARNING] ", indent=indent)
    
    def error(self, msg: str, indent: int = 0):
        """Error message"""
        self.log(msg, "[ERROR] ", indent=indent)

    def success(self, msg: str, indent: int = 0):
        """Success message"""
        self.log(msg, "[OK] ", indent=indent)

    def result_candidate(self, msg: str, indent: int = 0):
        """Match verdict: a clear upload candidate was found."""
        self.log(msg, "[Result: candidate] ", indent=indent)

    def result_possible_candidate(self, msg: str, indent: int = 0):
        """Match verdict: ambiguous -- worth a human look, not a known-good candidate."""
        self.log(msg, "[Result: possible candidate] ", indent=indent)

    def result_duplicate(self, msg: str, indent: int = 0):
        """Match verdict: target already has this; nothing to upload."""
        self.log(msg, "[Result: duplicate] ", indent=indent)

    def progress(self, msg: str, indent: int = 0):
        """Progress/framing message (task/page headers, pacing summaries,
        session banners) -- always cyan by construction, not by guessing at
        "[Task "/"[paced]"/etc in the message text. Permanent-line sibling
        of status()'s in-place version."""
        self.log(msg, indent=indent, style="cyan")


    def api_wait(self, tracker: str, seconds: float):
        """Log API rate limiting wait"""
        _ = seconds
        tracker_key = tracker.upper()
        if tracker_key in self._rate_limit_note_trackers:
            return
        self._rate_limit_note_trackers.add(tracker_key)
        self.log(
            f"API rate limiting active for {tracker_key}; request pacing is enabled.",
            "[INFO] ",
        )

    def api_wait_debug(self, tracker: str, seconds: float):
        """Log API wait details (debug mode only)."""
        self.debug(f"Rate limiting detail: waiting {seconds:.3f}s before next {tracker} API call")
    
    def api_retry(self, tracker: str, attempt: int, max_attempts: int, delay: int):
        """Log API retry"""
        self.log(f"{tracker} request retry in {delay}s... (attempt {attempt}/{max_attempts})", "[WARNING] ")

    def api_throttle(self, tracker: str, status: str = "429") -> None:
        """Log one-time tracker/service throttle warning."""
        tracker_key = tracker.upper()
        if tracker_key in self._throttle_note_trackers:
            return
        self._throttle_note_trackers.add(tracker_key)
        self.log(
            f"API throttling response from {tracker_key} ({status}). Are you running multiple oatgrass scripts? "
            "Consider --slow or a higher --slow value.",
            "[WARNING] ",
        )
    
    def api_failed(self, tracker: str, max_attempts: int):
        """Log API failure"""
        self.log(f"{tracker} server not responding after {max_attempts} attempts. Aborting.", "[ERROR] ")
    
    def debug(self, msg: str):
        """Debug message (only shown in debug mode)"""
        if self.debug_mode:
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            self.log(msg, f"[{timestamp}] [DEBUG] ")
    
    def api_request(self, method: str, url: str, params: dict):
        """Log API request (debug mode only)"""
        if self.debug_mode:
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            self.log(f"API Request: {method} {url}", f"[{timestamp}] ")
            if params:
                import json
                self.log(f"  Params: {json.dumps(params, indent=2)}", f"[{timestamp}] ")
    
    def api_response(self, status: int, data: dict, elapsed_ms: float):
        """Log API response (debug mode only)"""
        if self.debug_mode:
            timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
            self.log(f"API Response ({elapsed_ms:.0f}ms): Status {status}", f"[{timestamp}] ")
            if data:
                import json
                # Truncate large responses
                data_str = json.dumps(data, indent=2)
                if len(data_str) > 5000:
                    data_str = data_str[:5000] + "\n  ... (truncated)"
                self.log(f"  Data: {data_str}", f"[{timestamp}] ")
    
    @property
    def console(self) -> Console:
        """The one shared Rich Console, for callers that need to render a
        Rich object directly (e.g. a Table) rather than a plain log line --
        so a module never has a reason to construct its own Console()."""
        return self._console

    def close(self):
        """Close file handle with goodbye message"""
        self._clear_status_line()
        if self._file_handle:
            end_time = datetime.now()
            elapsed = end_time - self._start_time
            goodbye = f"({end_time.strftime('%H:%M:%S')}  Ended session, elapsed {elapsed.total_seconds():.1f}s)"
            self.progress(goodbye)
            self._file_handle.close()
            self._file_handle = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, *args):
        self.close()


# Global instance (set by search mode)
_logger: Optional[OatgrassLogger] = None

def set_logger(logger: OatgrassLogger):
    """Set global logger instance"""
    global _logger
    _logger = logger

def get_logger() -> OatgrassLogger:
    """Get global logger instance"""
    global _logger
    if _logger is None:
        # Fallback: create a stdout-only logger, silently -- callers reaching
        # this branch (e.g. a debug-log call from an atexit cleanup hook) are
        # not starting a session, so it must not print a startup banner.
        _logger = OatgrassLogger(quiet=True)
    return _logger

# Convenience functions
def log(msg: str, prefix: str = "", indent: int = 0):
    get_logger().log(msg, prefix=prefix, indent=indent)

def info(msg: str, indent: int = 0):
    get_logger().info(msg, indent=indent)

def warning(msg: str, indent: int = 0):
    get_logger().warning(msg, indent=indent)

def error(msg: str, indent: int = 0):
    get_logger().error(msg, indent=indent)

def success(msg: str, indent: int = 0):
    get_logger().success(msg, indent=indent)

def progress(msg: str, indent: int = 0):
    get_logger().progress(msg, indent=indent)


def mark_reported(exc: BaseException) -> BaseException:
    """Tag an exception as already having produced a user-facing message.

    Lets an outer catch (e.g. a per-entry handler in group_search.py or
    profile_search.py) skip re-announcing a failure a lower layer (e.g.
    gazelle_client.py's api_failed()) already logged at a different
    severity for the same underlying event.
    """
    exc._oatgrass_reported = True  # type: ignore[attr-defined]
    return exc


def was_reported(exc: BaseException) -> bool:
    """Whether mark_reported() was already called for this exception."""
    return bool(getattr(exc, "_oatgrass_reported", False))


# Exception types that almost always mean an oatgrass code defect reached a
# catch site, rather than an expected runtime condition (bad input, a
# throttle/network failure, an unavailable dependency) -- those are raised
# deliberately throughout oatgrass as ValueError/RuntimeError/OSError and
# friends, and already carry a human-written message. These four don't, so
# they must not be reported to the user with the same wording as a routine
# failure. Shared by cli.py's top-level fatal handler and any other catch
# site that needs to tell "expected failure" from "likely bug" apart.
INTERNAL_DEFECT_EXCEPTION_TYPES = (KeyError, AttributeError, TypeError, IndexError)


def is_internal_defect(exc: BaseException) -> bool:
    return isinstance(exc, INTERNAL_DEFECT_EXCEPTION_TYPES)
