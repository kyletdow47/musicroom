"""wall_links.py — the lines between things on the wall.

The wall is a mind map: any two blocks can be joined by a line. A line is yours ("mine": you drew it, solid)
or a suggestion ("near": it came with a grown web, like "sounds like", faint and dashed). It has two ends (block
ids) and an optional few words.

Nothing from the browser is trusted: this keeps only lines whose two blocks exist, drops repeats (a line has no
direction, so a→b is b→a), cuts the words short, and caps how many there are. Pure, so it can be tested.
"""

MAX_LINKS = 1500
MAX_LABEL = 80
KINDS = ("mine", "near")


def clean_links(links, blocks):
    """The lines worth keeping, given the blocks that exist. Always returns a list of {a, b, kind, label}."""
    ids = {b.get("id") for b in (blocks or []) if isinstance(b, dict) and isinstance(b.get("id"), str)}
    out, seen = [], {}
    for l in links if isinstance(links, list) else []:
        if not isinstance(l, dict):
            continue
        a, b = l.get("a"), l.get("b")
        if not (isinstance(a, str) and isinstance(b, str)) or a == b or a not in ids or b not in ids:
            continue
        key = (a, b) if a < b else (b, a)
        kind = l.get("kind") if l.get("kind") in KINDS else "mine"
        label = " ".join(str(l.get("label") or "").split())[:MAX_LABEL]
        if key in seen:                                   # the same pair twice: the person's own line wins, and a label is kept
            prev = out[seen[key]]
            if kind == "mine":
                prev["kind"] = "mine"
            if label and not prev["label"]:
                prev["label"] = label
            continue
        seen[key] = len(out)
        out.append({"a": a, "b": b, "kind": kind, "label": label})
        if len(out) >= MAX_LINKS:
            break
    return out


def next_number(stored, blocks):
    """The highest archive number ever given out on this wall: what was stored, or the highest on any block now, whichever is
    larger, so a number is never used twice even after its block is deleted. Always a whole number from 0 to a billion."""
    top = 0
    for b in blocks or []:
        n = b.get("n") if isinstance(b, dict) else None
        if isinstance(n, int) and not isinstance(n, bool) and 0 < n <= 10**9:
            top = max(top, n)
    try:
        st = int(stored) if not isinstance(stored, bool) else 0
    except (TypeError, ValueError):
        st = 0
    return max(top, min(max(st, 0), 10**9))
