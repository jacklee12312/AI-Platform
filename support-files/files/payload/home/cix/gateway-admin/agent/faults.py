"""Structured Houmo NPU fatal-log recognition shared by safety workflows."""
from __future__ import annotations

import re


FATAL = re.compile(
    r"ERR_FATAL|SyncYieldErr|group sync timeout|exec timeout!|"
    r"xh2a_ipu_sync_group failed|auto reset scheduled|indicator .*not synchronized",
    re.I,
)


def fatal_record(line: str) -> bool:
    """Recognize device faults while excluding an interrupted, planned stop.

    TCIM reports Linux EINTR as ret/hal_errno 4 when SIGTERM interrupts an
    in-flight group wait.  That record is expected while ModelDeck stops a
    container and is materially different from an NPU timeout.
    """
    text = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", line)
    kernel_record = re.match(r"^\S+\s+kernel:\s+\[[^\]]+\](.*)", text)
    if kernel_record:
        text = kernel_record[1]
    if not FATAL.search(text):
        return False

    if kernel_record and re.search(
        r"\bgroup \d+ .*exec timeout!$|\bgroup sync timeout$|"
        r"\bauto reset scheduled\b|\bindicator .*not synchronized",
        text,
        re.I,
    ):
        return True

    sync = re.match(r"xh2a_ipu_sync_group failed!\s+ret:(\d+)\b(.*)", text)
    if sync:
        result = int(sync[1])
        hal = re.search(r"\bhal_errno:(\d+)\b", sync[2])
        if result == 4 or (hal and int(hal[1]) == 4):
            return False
        return True

    if re.match(r"Device \d+ Core \d+ Tile \d+:.*indicator .*not synchronized", text):
        return True
    record = re.match(
        r"^\[\d{4}-\d\d-\d\d [^\]]+\] \[tid \d+\] "
        r"\[(?:error|critical|fatal)\] (.*)",
        text,
    )
    return bool(
        record
        and re.search(
            r"^\[Stream\] \[[^\]]+\] Model:\S+ DevID:\d+ SyncYieldErr:\d+\b"
            r"|^\S+\.(?:cc|cpp|h):\d+:\S+: .*tcim::Status::ERR_FATAL.*FAILED!",
            record[1],
        )
    )
