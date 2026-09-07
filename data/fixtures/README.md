# Vision fixtures

Photograph a pile once, run identification once, commit the JSON here. The
matcher, ranker and fallback chain are then developed against these fixtures
with **zero model calls** -- which keeps Bedrock spend near nothing and makes
the deterministic core unit-testable.

    pile_01.jpg        (gitignored -- keep locally)
    pile_01.json       (committed -- cached identification output)

Only end-to-end runs and the demo hit the real model.
