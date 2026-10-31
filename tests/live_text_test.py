"""Unit test for the client's live-text diffing. Run on the Mac: .venv/bin/python tests/live_text_test.py"""
import os, sys
sys.argv = [sys.argv[0], "--no-tunnel", "--no-overlay"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import client  # noqa: E402  (parses the argv above, imports AppKit)


class FakeKb:
    """Simulates a text field: records the characters the app would end up showing."""
    def __init__(self):
        self.buf = ""
        self.events = []

    def tap(self, key):
        assert key == client.keyboard.Key.backspace
        self.buf = self.buf[:-1]
        self.events.append("bs")

    def type(self, s):
        self.buf += s
        self.events.append(f"type:{s}")


def run(partials, final):
    kb = FakeKb()
    lt = client.LiveText(kb)
    for p in partials:
        lt.update(p)
        assert kb.buf == p, (kb.buf, p)
    pasted = []
    lt.replace(final, pasted.append)
    for t in pasted:
        kb.buf += t
    assert kb.buf == final, (kb.buf, final)
    assert lt.text == ""
    return kb


def main():
    # Typical hold: tail gets rewritten as ASR revises, committed prefix untouched.
    kb = run(["first update the", "First, update the read", "First, update the README with",
              "First, update the README with the new install steps"],
             "First, update the README with the new install steps. ")
    bs = kb.events.count("bs")
    assert bs <= 20, bs                       # only the diverging tails were erased (16 + 4)
    assert kb.events[-1] == "type:. "         # final differed by punctuation only, typed not pasted

    # Nothing in common: everything erased, long tail pasted.
    kb = run(["hello there how are you doing today"], "- Hello there, how are you doing today?\n- Second bullet\n")
    assert any(e.startswith("type:") for e in kb.events)
    assert kb.events.count("bs") == len("hello there how are you doing today")

    # Empty final clears what was typed.
    kb = FakeKb(); lt = client.LiveText(kb)
    lt.update("um so"); lt.clear()
    assert kb.buf == "" and lt.text == ""

    # Identical partial is a no-op.
    kb = FakeKb(); lt = client.LiveText(kb)
    lt.update("same"); n = len(kb.events); lt.update("same")
    assert len(kb.events) == n

    # Revisions far back (case, punctuation, a dropped word) must not drag the cursor back.
    base = "First, update the README with the new install steps. Second, email Sarah about the deadline, no wait, the budget."
    typed = base + " First, update the README with the new install"
    kb = FakeKb(); lt = client.LiveText(kb); lt.update(typed); kb.events.clear()
    flipped = base.replace("deadline, no wait", "deadline: No wait")
    lt.update(flipped + " First, update the README with the new install steps")
    assert kb.events.count("bs") == 0, kb.events                  # colon/case flip 60 chars back: ignored
    assert kb.buf == typed + " steps", kb.buf
    lt.update(flipped[7:] + " First, update the README with the new install steps. Second")
    assert kb.events.count("bs") == 0, kb.events                  # dropped first word: re-aligned, head kept
    assert kb.buf == typed + " steps. Second", kb.buf
    # ...but a change inside the live tail is applied.
    lt.update(flipped[7:] + " First, update the README with the new install steps. Third")
    assert kb.buf == typed + " steps. Third", kb.buf
    assert 0 < kb.events.count("bs") <= 6, kb.events

    # Final shorter than the live text (the formatter dropped a filler): only the tail is erased.
    kb = run(["so the build is um broken"], "So the build is broken. ")
    assert kb.events.count("bs") == len("so the build is um broken")   # 'S' differs at index 0
    kb = run(["the build is broken again"], "the build is broken ")
    assert kb.events.count("bs") == len("again") and kb.events[-1] == "bs", kb.events

    # Final identical to the live text: nothing is sent.
    kb = run(["ship it "], "ship it ")
    assert kb.events == ["type:ship it "], kb.events

    # No live text (no partial arrived): the whole final is typed, or pasted when long.
    kb = run([], "Short one. ")
    assert kb.events == ["type:Short one. "], kb.events
    kb = run([], "A final that is longer than the forty-eight character typing limit. ")
    assert not any(e.startswith("type:") for e in kb.events), kb.events
    kb = run([], "")
    assert kb.events == [], kb.events

    # Newlines are never typed, even in a short tail.
    kb = run(["one two"], "one\ntwo\n")
    assert not any(e.startswith("type:") and "\n" in e for e in kb.events), kb.events

    # Only the first letter's case differs: the app must show the exact final, so it is retyped.
    kb = run(["hello there"], "Hello there. ")
    assert kb.events.count("bs") == len("hello there")

    assert client.common_prefix_len("abc", "abd") == 2
    assert client.common_prefix_len("", "x") == 0
    print("live_text_test: ok")


if __name__ == "__main__":
    main()
