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

    assert client.common_prefix_len("abc", "abd") == 2
    assert client.common_prefix_len("", "x") == 0
    print("live_text_test: ok")


if __name__ == "__main__":
    main()
