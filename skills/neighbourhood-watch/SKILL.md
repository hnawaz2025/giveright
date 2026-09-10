---
name: neighbourhood-watch
description: Re-check items being held for a new need, and read what the neighbourhood is giving and short of. Use for background sweeps and for questions about trends.
allowed-tools:
  - check_held_items
  - neighbourhood_dashboard
---

# Watching, quietly

## Held items

When nothing would take an item, GiveRight told the donor it would hold it and
keep looking. **`check_held_items`** is that looking.

Report exactly two things: an item that now has a home, and a hold that has run
out. If it comes back `quiet`, **say nothing to the donor**. An agent that
reports "I checked and there was nothing" every morning is a notification
people turn off -- and then the one that mattered is off too.

Each hold carries the radius promised at the time. A later sweep cannot offer
somewhere the donor never agreed to travel.

## The dashboard

**`neighbourhood_dashboard`** is read by donors and organisations alike.

`recovery_rate` counts reuse by a named organisation. Recycling and disposal
are reported separately and are never folded in -- a metric that counts "here
is how to bin it" as a save cannot be wrong, and so means nothing.

A trend marked `significant: false` is noise. One donation last week and three
this week is not a 200% surge. Do not narrate it as one.
