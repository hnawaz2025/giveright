"""Which model does what, and how to change it without editing code.

Two roles, deliberately separate. Identification reads a photograph; the agent
runs an eleven-tool loop and must never invent an organisation's opening hours.
They have different failure modes, so they are allowed different models -- and
either can be swapped from the environment, so a model comparison is a shell
variable rather than a diff:

    GIVERIGHT_VISION_MODEL=us.amazon.nova-premier-v1:0 python -m giveright.demo --agent

Both default to Amazon Nova Pro on Bedrock. Nova is multimodal, needs no
per-account use case form, and is the family last year's winning waste-image
classifier used for very nearly this task. Nothing in the codebase depends on a
particular vendor: these are two strings and every entry point accepts an
injected model object instead.

The `us.` prefix selects a US cross-region inference profile. Some models --
Nova 2 Lite among them -- are only reachable that way, so it is used
consistently rather than only where it is strictly required.
"""

from __future__ import annotations

import os

# Multimodal, mid-tier, streaming. The default for both roles.
NOVA_PRO = "us.amazon.nova-pro-v1:0"

# Alternatives worth measuring against, all multimodal:
NOVA_PREMIER = "us.amazon.nova-premier-v1:0"   # most capable, most expensive
NOVA_2_LITE = "us.amazon.nova-2-lite-v1:0"     # newer generation, cheap
NOVA_LITE = "us.amazon.nova-lite-v1:0"

VISION_MODEL_ID = os.environ.get("GIVERIGHT_VISION_MODEL", NOVA_PRO)
AGENT_MODEL_ID = os.environ.get("GIVERIGHT_AGENT_MODEL", NOVA_PRO)


def bedrock(model_id: str):
    """A Bedrock model handle. Imported lazily so the deterministic core, the
    tests and the offline demo never need Strands' model layer or credentials."""
    from strands.models import BedrockModel

    return BedrockModel(model_id=model_id)
