"""What the agent did, in the order it did it.

An agent that acts on someone's behalf should be answerable for it. This is
that record: every tool the model chose, what it passed, how long it took, what
came back, and whether policy let it through.

It is a Strands `HookProvider`, so the recording happens in the framework
rather than inside each tool. That matters for honesty as much as tidiness --
a log written by the tools could only ever record what the tools chose to
admit, and would miss a call that was denied before it ran.

The file is append-only JSONL, like the observations and the holds. Three logs
now share that shape, and the reason is the same each time: the question people
ask afterwards is "what did it do, and when", and an append-only file answers
it while a mutable one does not.

Tool inputs are recorded, tool *results* are summarised. A drop-off plan is
several kilobytes of organisation detail that is already in the corpus; the log
needs to say a plan was produced, not reproduce it.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from strands.hooks import (
    AfterInvocationEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

AUDIT_FILE = Path(__file__).resolve().parents[2] / "data" / "audit.jsonl"

# Long values are cut rather than dropped: a truncated argument still shows
# what the model was trying to do, which is what the log is for.
MAX_VALUE = 300


@dataclass
class Entry:
    at: str
    run: str
    event: str                    # run_started | tool_called | tool_returned | run_finished
    tool: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    outcome: str | None = None    # ok | error | denied
    detail: str = ""
    ms: int | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, {}, "")}


def _trim(value: Any) -> Any:
    text = value if isinstance(value, (str, int, float, bool)) else json.dumps(
        value, default=str
    )
    text = str(text)
    return text if len(text) <= MAX_VALUE else text[:MAX_VALUE] + "…"


def _summarise(result: Any) -> str:
    """One line about what came back. Not the thing itself.

    Strands serialises a tool's dict return into a text block holding JSON, so
    the payload is unwrapped before summarising -- otherwise every line would
    be an escaped blob and the log would be unreadable, which is the same as
    not having one.
    """
    if result is None:
        return ""

    payload = _payload(result)
    if isinstance(payload, dict):
        if "error" in payload:
            return f"error: {_trim(payload['error'])}"
        return ", ".join(
            f"{k}={len(v) if isinstance(v, (list, dict)) else _trim(v)}"
            for k, v in list(payload.items())[:4]
        ) or "ok"
    return _trim(payload)


def _payload(result: Any) -> Any:
    """The tool's own return value, however Strands wrapped it."""
    content = result.get("content") if isinstance(result, dict) else None
    if not (isinstance(content, list) and content):
        return result

    first = content[0]
    if not isinstance(first, dict):
        return first
    if "json" in first:
        return first["json"]
    if "text" in first:
        try:
            return json.loads(first["text"])
        except (ValueError, TypeError):
            return first["text"]
    return first


class AuditLog(HookProvider):
    """Records the agent's actions. Never changes them."""

    def __init__(self, path: Path | None = None, run: str | None = None):
        self.path = path if path is not None else AUDIT_FILE
        self.run = run or uuid4().hex[:8]
        self.entries: list[Entry] = []
        self._started: dict[str, float] = {}

    # -- HookProvider -----------------------------------------------------

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._run_started)
        registry.add_callback(BeforeToolCallEvent, self._tool_called)
        registry.add_callback(AfterToolCallEvent, self._tool_returned)
        registry.add_callback(AfterInvocationEvent, self._run_finished)

    # -- callbacks --------------------------------------------------------

    def _run_started(self, event: BeforeInvocationEvent) -> None:
        self._write(Entry(at=_now(), run=self.run, event="run_started"))

    def _tool_called(self, event: BeforeToolCallEvent) -> None:
        use = event.tool_use or {}
        name = use.get("name", "?")
        self._started[use.get("toolUseId", name)] = time.perf_counter()
        self._write(
            Entry(
                at=_now(), run=self.run, event="tool_called", tool=name,
                inputs={k: _trim(v) for k, v in (use.get("input") or {}).items()},
            )
        )

    def _tool_returned(self, event: AfterToolCallEvent) -> None:
        use = event.tool_use or {}
        name = use.get("name", "?")
        key = use.get("toolUseId", name)
        started = self._started.pop(key, None)

        if event.exception is not None:
            outcome, detail = "error", _trim(str(event.exception))
        elif event.cancel_message:
            outcome, detail = "denied", _trim(event.cancel_message)
        else:
            outcome, detail = "ok", _summarise(event.result)

        self._write(
            Entry(
                at=_now(), run=self.run, event="tool_returned", tool=name,
                outcome=outcome, detail=detail,
                ms=int((time.perf_counter() - started) * 1000) if started else None,
            )
        )

    def _run_finished(self, event: AfterInvocationEvent) -> None:
        self._write(Entry(at=_now(), run=self.run, event="run_finished"))

    # -- storage ----------------------------------------------------------

    def _write(self, entry: Entry) -> None:
        self.entries.append(entry)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(entry.as_dict()) + "\n")

    # -- reading it back --------------------------------------------------

    @classmethod
    def load(cls, path: Path | None = None) -> list[dict]:
        path = path or AUDIT_FILE
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def timeline(self) -> list[str]:
        """The run as a person would read it."""
        lines = []
        for e in self.entries:
            if e.event == "tool_called":
                shown = ", ".join(f"{k}={v}" for k, v in list(e.inputs.items())[:3])
                lines.append(f"  → {e.tool}({shown})")
            elif e.event == "tool_returned":
                mark = {"ok": "✓", "denied": "✗", "error": "!"}.get(e.outcome, "?")
                took = f" [{e.ms}ms]" if e.ms is not None else ""
                lines.append(f"    {mark} {e.detail}{took}")
        return lines


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
