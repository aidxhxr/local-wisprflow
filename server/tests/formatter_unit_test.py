"""Unit test for the formatter's pure-Python parts: app categorisation, the regex fallback,
output post-processing and prompt assembly. No LLM, GPU or network needed; only httpx must be
importable. Run anywhere: python server/tests/formatter_unit_test.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import formatter  # noqa: E402
from formatter import categorize, clean, _postprocess, _user_msg  # noqa: E402

_failures = []


def eq(got, want, what):
    if got != want:
        _failures.append(f"{what}: got {got!r}, want {want!r}")


def test_categorize():
    eq(categorize("com.apple.Terminal", "Terminal", "zsh"), "terminal", "terminal bundle")
    eq(categorize("com.apple.Terminal", "Terminal", "✳ Claude Code"), "ai_chat", "agent in a terminal")
    eq(categorize("com.googlecode.iterm2", "iTerm2", "codex — ~/proj"), "ai_chat", "codex in iTerm")
    eq(categorize("com.microsoft.VSCode", "Code"), "code", "vscode")
    eq(categorize("com.jetbrains.pycharm", "PyCharm"), "code", "jetbrains prefix match")
    eq(categorize("com.tinyspeck.slackmacgap", "Slack", "#eng"), "chat", "slack")
    eq(categorize("com.apple.mail", "Mail"), "email", "mail")
    eq(categorize("com.anthropic.claudefordesktop", "Claude"), "ai_chat", "claude desktop")
    eq(categorize("md.obsidian", "Obsidian"), "document", "obsidian")
    # Browsers: the window title decides.
    eq(categorize("com.google.Chrome", "Chrome", "ChatGPT"), "ai_chat", "chatgpt tab")
    eq(categorize("com.apple.Safari", "Safari", "Inbox (3) - Gmail"), "email", "gmail tab")
    eq(categorize("company.thebrowser.Browser", "Arc", "Pull Request #12 · GitHub"), "code", "github tab")
    eq(categorize("org.mozilla.firefox", "Firefox", "Sprint board - Jira"), "document", "jira tab")
    eq(categorize("com.google.Chrome", "Chrome", "Zillow"), "browser", "unknown tab")
    eq(categorize("com.google.Chrome", "Chrome", ""), "browser", "browser, no title")
    # Unknown or missing bundle.
    eq(categorize("com.example.unknown", "Thing"), "other", "unknown bundle")
    eq(categorize("", "", ""), "other", "empty context")
    eq(categorize(None, None, None), "other", "None context")


def test_clean():
    eq(clean(""), "", "empty")
    eq(clean(None), "", "None")
    eq(clean("   "), "", "whitespace only")
    eq(clean("um, so uh we could, erm, ship it"), "So we could, ship it", "fillers removed")
    eq(clean("hmm. okay"), "Okay", "lone hmm")
    eq(clean("hello   world ,  how are you ?"), "Hello world, how are you?", "spacing around punctuation")
    eq(clean(", and then"), "And then", "leading punctuation dropped")
    eq(clean("summer is here"), "Summer is here", "'um' inside a word is kept")
    eq(clean("the hummus"), "The hummus", "'hmm' inside a word is kept")
    eq(clean("line one\nline two"), "Line one line two", "newlines collapsed")


def test_postprocess():
    eq(_postprocess('"Hello there."', "hello there", ""), "Hello there.", "wrapping double quotes stripped")
    eq(_postprocess('"Hello," she said.', "quote hello unquote she said", ""), '"Hello," she said.',
       "quotes that do not wrap the whole output stay")
    eq(_postprocess('"Hi"', '"Hi"', ""), '"Hi"', "quotes kept when the transcript starts with one")
    eq(_postprocess("Output: Hello there.", "hello there", ""), "Hello there.", "'output:' label stripped")
    eq(_postprocess("text: Hello", "hello", ""), "Hello", "'text:' label stripped")
    eq(_postprocess("  a line  \n  another   \n", "x", ""), "a line\n  another", "trailing whitespace per line")
    eq(_postprocess("", "x", ""), "", "empty output")


def test_user_msg():
    eq(_user_msg("chat", "", "", "", "hi"), "context: chat\ntranscript:\n<<<\nhi\n>>>", "minimal")
    eq(_user_msg("chat", "Slack", "", "", "hi").splitlines()[0], "context: chat (Slack)", "app in context")
    msg = _user_msg("code", "Cursor", "client.py — repo", "", "hi")
    eq(msg.splitlines()[0], "context: code (Cursor) — window: 'client.py — repo'", "title in context")
    msg = _user_msg("other", "", "t" * 500, "", "hi")
    eq(("t" * 120 in msg, "t" * 121 in msg), (True, False), "title truncated to 120 chars")
    msg = _user_msg("document", "Notes", "", "old text", "make it shorter")
    eq(msg, "context: document (Notes)\nselected text:\n<<<\nold text\n>>>\ntranscript:\n<<<\nmake it shorter\n>>>",
       "selected text block before the transcript")
    eq("x" * 4000 in _user_msg("other", "", "", "x" * 5000, "hi"), True, "selection truncated to 4000 chars")
    eq("x" * 4001 in _user_msg("other", "", "", "x" * 5000, "hi"), False, "selection truncated to 4000 chars")


def test_messages():
    msgs = formatter._messages("chat", "Slack", "#eng", "", "hello")
    eq(msgs[0]["role"], "system", "system first")
    eq(msgs[-1]["role"], "user", "live request last")
    eq(len(msgs), 2 + 2 * len(formatter.FEWSHOT), "one user/assistant pair per few-shot example")
    roles = [m["role"] for m in msgs[1:-1]]
    eq(roles, ["user", "assistant"] * len(formatter.FEWSHOT), "few-shot roles alternate")
    eq("transcript:\n<<<\nhello\n>>>" in msgs[-1]["content"], True, "live transcript in last message")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    if _failures:
        print("\n".join(_failures))
        print(f"formatter_unit_test: {len(_failures)} failure(s)")
        sys.exit(1)
    print("formatter_unit_test: ok")


if __name__ == "__main__":
    main()
