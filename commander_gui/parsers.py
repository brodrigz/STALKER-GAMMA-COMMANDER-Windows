r"""Parsers for the stalker-gamma CLI console output.

The CLI emits Serilog-formatted lines. Progress lines look like:

    \e[97m[12:00:00]\e[0m \e[96mAddonName\e[0m ... | Download | 12.34% | [3/250]

and (with --verbose) omit the timestamp and append the URL. All parsers operate
on ANSI-stripped text.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

# Informational progress line: [HH:mm:ss] Addon | Operation | Percent | [C/T]
INFORMATIONAL_PROGRESS_RE = re.compile(
    r"^\[(\d{2}:\d{2}:\d{2})\]\s+"
    r"(?P<name>.+?)\s*\|\s*"
    r"(?P<op>Download|Extract|Expand|Check MD5|Skipped)\s*\|\s*"
    r"(?P<percent>\d+(?:[.,]\d+)?)\s*%\s*\|\s*"
    r"\[(?P<complete>\d+)/(?P<total>\d+)\]$"
)
# Verbose progress line: Addon | Operation | Percent | [C/T] [| Url]
VERBOSE_PROGRESS_RE = re.compile(
    r"^(?P<name>.+?)\s*\|\s*"
    r"(?P<op>Download|Extract|Expand|Check MD5|Skipped)\s*\|\s*"
    r"(?P<percent>\d+(?:[.,]\d+)?)\s*%\s*\|\s*"
    r"\[(?P<complete>\d+)/(?P<total>\d+)\]\s*(?:\|.*)?$"
)

PRUNE_ARCHIVE_RE = re.compile(
    r"^(?P<file>.+?)\s*\|\s*(?P<mb>\d+)mb\s*\|\s*(?P<date>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})$"
)


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


@dataclass(frozen=True)
class ProgressEvent:
    name: str
    operation: str
    percent: float
    complete: int
    total: int
    bytes_downloaded: int | None = None
    total_bytes: int | None = None
    bytes_per_second: float | None = None
    structured: bool = False


PROGRESS_PREFIX = "@commander-progress "
_PROGRESS_OPERATIONS = {
    "Download", "Extract", "Expand", "Check MD5", "Skipped", "Complete",
    "Queued", "Resolving", "Retrying", "Resuming", "Verifying", "Downloaded", "Failed",
}


def _structured_progress(line: str) -> ProgressEvent | None:
    try:
        data = json.loads(line[len(PROGRESS_PREFIX):])
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            return None
        if not isinstance(data.get("name"), str) or not data["name"].strip():
            return None
        if not isinstance(data.get("operation"), str) or data["operation"] not in _PROGRESS_OPERATIONS:
            return None
        for key in ("complete", "total", "bytesDownloaded", "totalBytes"):
            value = data.get(key)
            if value is None and key in ("bytesDownloaded", "totalBytes"):
                continue
            if type(value) is not int or value < 0:
                return None
        for key in ("percent", "bytesPerSecond"):
            value = data.get(key)
            if value is None and key == "bytesPerSecond":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                return None
        return ProgressEvent(data["name"], data["operation"], min(1.0, data["percent"]),
                             data["complete"], data["total"], data.get("bytesDownloaded"),
                             data.get("totalBytes"), data.get("bytesPerSecond"), True)
    except (ValueError, TypeError, OverflowError):
        return None


def parse_progress_line(line: str) -> ProgressEvent | None:
    """Parse a single progress line; returns None if the line isn't progress."""
    if line.startswith(PROGRESS_PREFIX):
        return _structured_progress(line)
    match = INFORMATIONAL_PROGRESS_RE.match(line) or VERBOSE_PROGRESS_RE.match(line)
    if not match:
        return None
    percent = float(match.group("percent").replace(",", ".")) / 100.0
    return ProgressEvent(
        name=match.group("name").rstrip(),
        operation=match.group("op"),
        percent=percent,
        complete=int(match.group("complete")),
        total=int(match.group("total")),
    )


@dataclass(frozen=True)
class PruneArchive:
    file: str
    mb: int
    date: str


def parse_prune_archive(line: str) -> PruneArchive | None:
    match = PRUNE_ARCHIVE_RE.match(line)
    if not match:
        return None
    return PruneArchive(
        file=match.group("file").strip(),
        mb=int(match.group("mb")),
        date=match.group("date"),
    )


@dataclass(frozen=True)
class UpdateDiff:
    status: str  # Modified / Added / Removed
    text: str
    detail: str = ""  # human-readable "Archive change" cell text (Modified only)
    detail_tooltip: str = ""  # raw technical detail shown on hover, if any
