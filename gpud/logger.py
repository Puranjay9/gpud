"""
Pretty logger using only Python stdlib (no rich/loguru required).
"""

import sys
import threading
from datetime import datetime

_LOCK = threading.Lock()

# ANSI codes
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RED    = "\033[91m"
GREEN  = "\033[92m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
CYAN   = "\033[96m"
WHITE  = "\033[97m"
GREY   = "\033[90m"

LEVEL_STYLES = {
    "DEBUG": (GREY,   " DBG"),
    "INFO":  (CYAN,   "INFO"),
    "WARN":  (YELLOW, "WARN"),
    "ERROR": (RED,    " ERR"),
    "OK":    (GREEN,  "  OK"),
}

_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3, "OK": 1}
_MIN_LEVEL   = "DEBUG"  # change to DEBUG for verbose output


def _supports_color() -> bool:
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()


class _Logger:
    def __init__(self):
        self._color = _supports_color()

    def _emit(self, level: str, msg: str):
        if _LEVEL_ORDER.get(level, 1) < _LEVEL_ORDER.get(_MIN_LEVEL, 1):
            return
        ts    = datetime.now().strftime("%H:%M:%S")
        style, tag = LEVEL_STYLES.get(level, (WHITE, level[:4]))
        if self._color:
            line = f"{GREY}{ts}{RESET} {style}{BOLD}{tag}{RESET} {msg}"
        else:
            line = f"{ts} [{tag}] {msg}"
        with _LOCK:
            print(line, file=sys.stderr, flush=True)

    def debug(self, msg: str): self._emit("DEBUG", msg)
    def info(self,  msg: str): self._emit("INFO",  msg)
    def warning(self, msg: str): self._emit("WARN", msg)
    def warn(self,  msg: str): self._emit("WARN",  msg)
    def error(self, msg: str): self._emit("ERROR", msg)
    def ok(self,    msg: str): self._emit("OK",    msg)

    def set_debug(self):
        global _MIN_LEVEL
        _MIN_LEVEL = "DEBUG"


log = _Logger()
