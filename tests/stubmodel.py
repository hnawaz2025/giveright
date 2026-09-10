"""A Strands model that says exactly what you tell it to.

The agent loop was the one part of this project no test touched: 142 tests
drove the tools directly, so nothing proved the tools were registered, that the
interrupt round-trip worked, or that a hook ever fired. Bedrock being
unavailable made that gap permanent rather than temporary.

This closes it. `StubModel` implements the four abstract methods of
`strands.models.Model` and emits the Bedrock Converse stream shape from a
script, so the real `Agent` -- real registry, real hooks, real interventions --
runs end to end with no credentials and no network.
"""

from __future__ import annotations

import json
from typing import Any

from strands.models.model import Model


def use(name: str, **inputs) -> dict:
    """A scripted turn in which the model calls one tool."""
    return {"tool": name, "input": inputs}


def say(text: str) -> dict:
    """A scripted turn in which the model answers and stops."""
    return {"text": text}


class StubModel(Model):
    """Replays `script`, one turn per model call. Records what it was asked."""

    def __init__(self, script: list[dict] | None = None):
        self.script = list(script or [])
        self.turn = 0
        self.seen: list[list] = []          # messages passed on each call
        self.tool_specs: list = []

    # -- the parts Strands requires ---------------------------------------

    def get_config(self) -> Any:
        return {"model_id": "stub"}

    def update_config(self, **model_config: Any) -> None:
        pass

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        yield {"output": output_model()}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.seen.append(messages)
        self.tool_specs = tool_specs or []

        turn = (
            self.script[self.turn]
            if self.turn < len(self.script)
            else say("Nothing further.")
        )
        self.turn += 1

        yield {"messageStart": {"role": "assistant"}}

        if "tool" in turn:
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {
                        "toolUse": {
                            "toolUseId": f"stub-{self.turn}",
                            "name": turn["tool"],
                        }
                    },
                }
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"toolUse": {"input": json.dumps(turn["input"])}},
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"text": turn["text"]},
                }
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
