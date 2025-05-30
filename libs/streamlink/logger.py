from __future__ import annotations

import logging
import sys
import warnings
from collections.abc import Iterator
from logging import CRITICAL, DEBUG, ERROR, INFO, WARNING
from pathlib import Path
from sys import version_info
from threading import Lock
from typing import IO, TYPE_CHECKING, Literal

# noinspection PyProtectedMember
from warnings import WarningMessage

from streamlink.exceptions import StreamlinkWarning
from streamlink.utils.times import fromlocaltimestamp


if TYPE_CHECKING:
    _BaseLoggerClass = logging.Logger
else:
    _BaseLoggerClass = logging.getLoggerClass()


class StreamlinkLogger(_BaseLoggerClass):
    def iter(self, level: int, messages: Iterator[str], *args, **kwargs) -> Iterator[str]:
        """
        Iterator wrapper for logging multiple items in a single call and checking log level only once
        """

        if not self.isEnabledFor(level):
            yield from messages

        for message in messages:
            self._log(level, message, args, **kwargs)
            yield message


FORMAT_STYLE: Literal["%", "{", "$"] = "{"
FORMAT_BASE = "[{name}][{levelname}] {message}"
FORMAT_DATE = "%H:%M:%S"
REMOVE_BASE = ["streamlink", "streamlink_cli"]

# Make NONE ("none") the highest possible level that suppresses all log messages:
#  `logging.NOTSET` (equal to 0) can't be used as the "none" level because of `logging.Logger.getEffectiveLevel()`, which
#  loops through the logger instance's ancestor chain and checks whether the instance's level is NOTSET. If it is NOTSET,
#  then it continues with the parent logger, which means that if the level of `streamlink.logger.root` was set to "none" and
#  its value NOTSET, then it would continue with `logging.root` whose default level is `logging.WARNING` (equal to 30).
NONE = sys.maxsize
# Add "trace" and "all" to Streamlink's log levels
TRACE = 5
ALL = 2

# Define Streamlink's log levels (and register both lowercase and uppercase names)
_levelToNames = {
    NONE: "none",
    CRITICAL: "critical",
    ERROR: "error",
    WARNING: "warning",
    INFO: "info",
    DEBUG: "debug",
    TRACE: "trace",
    ALL: "all",
}

_custom_levels = TRACE, ALL


def _logmethodfactory(level: int, name: str):
    # fix module name that gets read from the call stack in the logging module
    # https://github.com/python/cpython/commit/5ca6d7469be53960843df39bb900e9c3359f127f
    if version_info >= (3, 11):

        def method(self, message, *args, **kws):
            if self.isEnabledFor(level):
                # increase the stacklevel by one and skip the `trace()` call here
                kws["stacklevel"] = 2
                self._log(level, message, args, **kws)

    else:

        def method(self, message, *args, **kws):
            if self.isEnabledFor(level):
                self._log(level, message, args, **kws)

    method.__name__ = name
    return method


for _level, _name in _levelToNames.items():
    logging.addLevelName(_level, _name.upper())
    logging.addLevelName(_level, _name)

    if _level in _custom_levels:
        setattr(StreamlinkLogger, _name, _logmethodfactory(_level, _name))


_config_lock = Lock()


class StringFormatter(logging.Formatter):
    def __init__(self, *args, remove_base: list[str] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._remove_base = remove_base or []
        self._usesTime = super().usesTime()

        # Validate the format's fields
        rec = logging.LogRecord("", 1, "", 1, "", None, None)
        super().format(rec)

    def usesTime(self):
        return self._usesTime

    def formatTime(self, record, datefmt=None):
        tdt = fromlocaltimestamp(record.created)

        return tdt.strftime(datefmt or self.default_time_format)

    def format(self, record):
        for rbase in self._remove_base:
            record.name = record.name.replace(f"{rbase}.", "")
        record.levelname = record.levelname.lower()

        return super().format(record)


class StreamHandler(logging.StreamHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stream_reconfigure()

    def flush(self):
        try:
            super().flush()
        except OSError:
            # Python doesn't raise BrokenPipeError on Windows
            pass

    def setStream(self, stream):
        res = super().setStream(stream)
        if res:  # pragma: no branch
            self._stream_reconfigure()

        return res

    def _stream_reconfigure(self):
        # make stream write calls escape unsupported characters (stdout/stderr encoding is not guaranteed to be utf-8)
        self.stream.reconfigure(errors="backslashreplace")


class WarningLogRecord(logging.LogRecord):
    msg: WarningMessage  # type: ignore[assignment]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.name = "warnings"
        self.levelname = self.msg.category.__name__ if self.msg.category else UserWarning.__name__
        self.pathname = self.msg.filename
        self._path = Path(self.pathname)
        self.filename = self._path.name
        self.module = self._path.stem
        self.lineno = self.msg.lineno

    def getMessage(self) -> str:
        if self.msg.category and issubclass(self.msg.category, StreamlinkWarning):
            return f"{self.msg.message}"
        return f"{self.msg.message}\n  {self.pathname}:{self.lineno}"


def _log_record_factory(name, level, fn, lno, msg, args, exc_info, func=None, sinfo=None, **kwargs):
    if isinstance(msg, WarningMessage):
        # noinspection PyTypeChecker
        return WarningLogRecord(name, level, fn, lno, msg, args, exc_info, func, sinfo)

    return _log_record_factory_default(name, level, fn, lno, msg, args, exc_info, func=None, sinfo=None, **kwargs)


# borrowed from stdlib and modified, so that `WarningMessage` gets passed as `msg` to the `WarningLogRecord`
def _showwarning(message, category, filename, lineno, file=None, line=None):
    if file is not None:  # pragma: no cover
        if _showwarning_default is not None:
            # noinspection PyCallingNonCallable
            _showwarning_default(message, category, filename, lineno, file, line)
        return

    warning = WarningMessage(message, category, filename, lineno, None, line)
    root.log(WARNING, warning, stacklevel=2)


def capturewarnings(capture=False):
    global _showwarning_default  # noqa: PLW0603

    if capture:
        if _showwarning_default is None:
            _showwarning_default = warnings.showwarning
            warnings.showwarning = _showwarning
    else:
        if _showwarning_default is not None:
            warnings.showwarning = _showwarning_default
            _showwarning_default = None


# noinspection PyShadowingBuiltins,PyPep8Naming
def basicConfig(
    *,
    filename: str | Path | None = None,
    filemode: str = "a",
    format: str = FORMAT_BASE,  # noqa: A002
    datefmt: str = FORMAT_DATE,
    style: Literal["%", "{", "$"] = FORMAT_STYLE,
    level: str | None = None,
    stream: IO | None = None,
    remove_base: list[str] | None = None,
    capture_warnings: bool = False,
) -> logging.StreamHandler | None:
    with _config_lock:
        handler: logging.StreamHandler | None = None
        if filename is not None:
            handler = logging.FileHandler(filename, filemode, encoding="utf-8")
        elif stream is not None:
            handler = StreamHandler(stream)

        if handler is not None:
             formatter = StringFormatter(
                 fmt=format,
                 datefmt=datefmt,
                 style=style,
                 remove_base=remove_base or REMOVE_BASE,
             )
             handler.setFormatter(formatter)
 
             root.addHandler(handler)

        if level is not None:
            root.setLevel(level)

        if capture_warnings:
            capturewarnings(True)

    return handler


_showwarning_default = None
_log_record_factory_default = logging.getLogRecordFactory()
logging.setLogRecordFactory(_log_record_factory)


logging.setLoggerClass(StreamlinkLogger)
root = logging.getLogger("streamlink")
root.setLevel(WARNING)

levels = list(_levelToNames.values())


__all__ = [
    "NONE",
    "CRITICAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
    "TRACE",
    "ALL",
    "StreamlinkLogger",
    "basicConfig",
    "root",
    "levels",
    "capturewarnings",
]
