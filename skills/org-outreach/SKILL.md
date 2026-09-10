---
name: org-outreach
description: Ask an organisation whether an aged need is still live, and record what they say. Use when a plan reports verification_offers, or when an organisation replies.
allowed-tools:
  - message_org
  - verify_with_org
  - record_org_reply
---

# Talking to organisations

Needs go stale. The corpus knows exactly how old each one is, and the honest
response to an old fact is to go and check rather than to quietly trust it less.

## Asking

**`verify_with_org`** drafts an email carrying the donor's actual items and
shows it to them to approve or add to before anything is sent. Nobody answers
"please confirm your inventory"; people answer "someone has four towels for
you, do you still want them?".

Email is the only channel. You never place calls. If the donor wants to ring
somewhere, give them the number from the plan -- they are a neighbour, not an
automated system.

You may only contact organisations that are on the plan. There is nothing to
ask someone nobody is being sent to, and the policy will refuse it.

## Recording

**`record_org_reply`** takes one of `take_these`, `full`, `dont_take`,
`no_answer`. These are different facts and are stored differently:

- `full` is temporary and clears.
- `dont_take` is permanent and removes the category **for every future donor**.
  You will be asked to confirm it. Only say yes if they actually said so.

Sending a question is not an answer. Nothing about the corpus changes because
you asked -- it changes when they reply.
