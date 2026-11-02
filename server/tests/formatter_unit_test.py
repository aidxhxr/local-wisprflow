"""Unit test for the formatter's pure-Python parts: app categorisation, the regex fallback,
output post-processing and prompt assembly. No LLM, GPU or network needed; only httpx must be
importable. Run anywhere: python server/tests/formatter_unit_test.py
"""
import asyncio, logging, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import httpx  # noqa: E402
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
    eq(categorize("com.barebones.bbedit", "BBEdit"), "code", "bbedit")
    eq(categorize("org.gnu.Emacs", "Emacs"), "code", "emacs")
    eq(categorize("com.tinyspeck.slackmacgap", "Slack", "#eng"), "chat", "slack")
    eq(categorize("com.apple.mail", "Mail"), "email", "mail")
    eq(categorize("com.anthropic.claudefordesktop", "Claude"), "ai_chat", "claude desktop")
    eq(categorize("md.obsidian", "Obsidian"), "document", "obsidian")
    eq(categorize("com.linear", "Linear"), "document", "linear app")
    eq(categorize("com.figma.Desktop", "Figma"), "document", "figma app")
    eq(categorize("com.apple.iWork.Keynote", "Keynote"), "document", "iwork prefix match")
    eq(categorize("com.microsoft.Powerpoint", "PowerPoint"), "document", "powerpoint")
    eq(categorize("net.shinyfrog.bear", "Bear"), "document", "bear 2")
    eq(categorize("Mattermost.Desktop", "Mattermost"), "chat", "mattermost app")
    eq(categorize("company.thebrowser.Browser", "Arc", "T3 Chat - t3.chat"), "ai_chat", "t3.chat tab")
    eq(categorize("com.google.Chrome", "Chrome", "Linear - linear.app"), "document", "linear tab")
    eq(categorize("com.google.Chrome", "Chrome", "Untitled - Figma"), "document", "figma tab")
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
    eq(clean("move it to to Thursday"), "Move it to Thursday", "stutter collapsed")
    eq(clean("get the the the new deck"), "Get the new deck", "triple stutter collapsed")
    eq(clean("no, no, I meant Friday"), "No, I meant Friday", "stutter across a comma collapsed")
    eq(clean("that is what I I said"), "That is what I I said", "one-letter repeats kept")
    eq(clean("there there, it is fine"), "There, it is fine", "case-insensitive repeat")
    eq(clean("the theme is dark"), "The theme is dark", "prefix of the next word is not a repeat")


def test_postprocess():
    eq(_postprocess('"Hello there."', "hello there", ""), "Hello there.", "wrapping double quotes stripped")
    eq(_postprocess('"Hello," she said.', "quote hello unquote she said", ""), '"Hello," she said.',
       "quotes that do not wrap the whole output stay")
    eq(_postprocess('"Hi"', '"Hi"', ""), '"Hi"', "quotes kept when the transcript starts with one")
    eq(_postprocess("Output: Hello there.", "hello there", ""), "Hello there.", "'output:' label stripped")
    eq(_postprocess("text: Hello", "hello", ""), "Hello", "'text:' label stripped")
    eq(_postprocess("  a line  \n  another   \n", "x", ""), "a line\n  another", "trailing whitespace per line")
    eq(_postprocess("“Hello there.”", "hello there", ""), "Hello there.", "wrapping smart quotes stripped")
    eq(_postprocess("“Hello there.", "hello there", ""), "“Hello there.", "lone opening quote stays")
    eq(_postprocess('"""Returns the user."""', "triple quote returns the user", ""), '"""Returns the user."""',
       "docstring quotes are not a wrapping pair")
    eq(_postprocess("<think>\nThe user wants prose.\n</think>\n\nHello there.", "hello there", ""),
       "Hello there.", "leaked think block stripped")
    eq(_postprocess("<think>\n\n</think>Hello", "hello", ""), "Hello", "empty think block stripped")
    eq(_postprocess("<think>still reasoning when max_tokens hit", "hello", ""), "",
       "unterminated think block is not typed")
    eq(_postprocess("context: chat (Slack)\ntranscript:\n<<<\nHello there\n>>>", "hello there", ""),
       "Hello there", "echoed prompt scaffolding stripped")
    eq(_postprocess("transcript: Hello there", "hello there", ""), "Hello there", "'transcript:' label stripped")
    eq(_postprocess("Context: I think we should go.", "context I think we should go", ""),
       "Context: I think we should go.", "'context:' is only an echo when followed by a transcript label")
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


def test_timeout():
    base = formatter.LLM_TIMEOUT
    eq(formatter._timeout_for(""), base, "empty transcript keeps the base timeout")
    eq(formatter._timeout_for("hello there"), base + 11 / formatter._TIMEOUT_CHARS_PER_S, "short transcript")
    two_minutes = "w" * 1800
    eq(formatter._timeout_for(two_minutes), base + 6.0, "two minutes of speech gets six extra seconds")
    eq(formatter._timeout_for("hi", "s" * 300), base + 302 / formatter._TIMEOUT_CHARS_PER_S,
       "selected text counts too")


def test_vocabulary():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "vocab.txt")
        v = formatter._VocabFile(path)
        eq(v.read(), [], "missing file is an empty vocabulary")
        with open(path, "w") as fh:
            fh.write("# people\nSwarthmore\n\nParakeet  # the ASR model\nvLLM\nC#\nSwarthmore\n")
        eq(v.read(), ["Swarthmore", "Parakeet", "vLLM", "C#"], "comments, blanks and duplicates dropped")
        with open(path, "w") as fh:
            fh.write("Qwen\n")
        os.utime(path, (1, 1))   # force a different mtime even on a coarse filesystem clock
        eq(v.read(), ["Qwen"], "re-read when the mtime changes")
        with open(path, "w") as fh:
            fh.write("\n".join(f"t{i}" for i in range(500)))
        os.utime(path, (2, 2))
        eq(len(v.read()), formatter.VOCAB_MAX_TERMS, "capped")
        os.remove(path)
        eq(v.read(), [], "deleted file clears the vocabulary")

    eq(_user_msg("chat", "", "", "", "hi", ["Swarthmore", "vLLM"]),
       "context: chat\nvocabulary: Swarthmore, vLLM\ntranscript:\n<<<\nhi\n>>>", "vocabulary line after context")
    eq(_user_msg("chat", "", "", "", "hi", []), "context: chat\ntranscript:\n<<<\nhi\n>>>", "empty vocabulary omitted")
    msgs = formatter._messages("chat", "", "", "", "hi", ["Swarthmore"])
    eq("vocabulary:" in msgs[-1]["content"], True, "live request carries the vocabulary")
    eq(any("vocabulary:" in m["content"] for m in msgs[1:-1]), False, "few-shot examples do not")
    eq('"vocabulary:"' in formatter.SYSTEM, True, "system prompt explains the vocabulary line")


class FakeClient:
    """Stands in for httpx.AsyncClient: raises the queued exceptions first, then answers `reply`."""
    def __init__(self, reply, errors=()):
        self.reply, self.errors, self.calls = reply, list(errors), []

    async def post(self, url, json, timeout):
        self.calls.append((json, timeout))
        if self.errors:
            raise self.errors.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": self.reply}}]},
                              request=httpx.Request("POST", url))


def run_format(transcript, context, reply, errors=()):
    f = formatter.Formatter()
    f.client = FakeClient(reply, errors)
    return asyncio.run(f.format(transcript, context)), f.client


def test_format():
    r, c = run_format("um hello there", {"category": "chat", "app": "Slack"}, "Hello there")
    eq((r["text"], r["raw"], r["used_llm"], r["category"]), ("Hello there", "Hello there", True, "chat"),
       "formatted output used")
    eq(c.calls[0][0]["messages"][-1]["content"].endswith("transcript:\n<<<\num hello there\n>>>"), True,
       "untouched transcript goes to the model")
    eq(c.calls[0][1], formatter._timeout_for("um hello there"), "scaled timeout passed per request")
    r, _ = run_format("um. uh", {"category": "other"}, "SHOULD NOT BE CALLED")
    eq((r["text"], r["used_llm"]), ("", False), "filler-only transcript skips the LLM")
    r, _ = run_format("hello", {"bundle": "com.apple.Terminal", "title": "zsh"}, "hello")
    eq(r["category"], "terminal", "category derived from the bundle when not given")


def test_length_guard():
    prose = "so I was thinking we could maybe ship the new version on monday and do the announcement on friday"
    r, _ = run_format(prose, {"category": "other"}, "Ship Monday.")
    eq(r["used_llm"], False, "collapsed plain dictation falls back to raw")
    r, _ = run_format(prose, {"category": "other"}, prose * 3)
    eq(r["used_llm"], False, "ballooned plain dictation falls back to raw")
    r, _ = run_format(prose, {"category": "other"}, "So I was thinking we could maybe ship the new version on Monday "
                      "and do the announcement on Friday.")
    eq(r["used_llm"], True, "ordinary formatting passes")
    r, _ = run_format("yeah I'm down for tonight. scratch that. actually I can't make it", {"category": "chat"},
                      "Actually I can't make it")
    eq(r["used_llm"], True, "'scratch that' may shorten the output a lot")
    r, _ = run_format("things to buy colon bullet milk bullet eggs bullet coffee beans new paragraph call the dentist",
                      {"category": "document"}, "Buy:\n- Milk\n- Eggs")
    eq(r["used_llm"], True, "dictated bullets and paragraphs switch the guard off")
    r, _ = run_format("step one numbered list open the app number one tap settings", {"category": "document"},
                      "1. Open\n2. Tap")
    eq(r["used_llm"], True, "numbered list switches the guard off")
    r, _ = run_format("a" * 50, {"category": "other", "selected_text": "some selection"}, "x")
    eq(r["used_llm"], True, "editing a selection switches the guard off")


def test_code_guard():
    code = {"category": "code", "app": "Cursor"}
    r, _ = run_format("add a comment that says this is a hack", code, "# this is a hack")
    eq((r["text"], r["used_llm"]), ("Add a comment that says this is a hack", False), "unspoken '#' refused")
    r, _ = run_format("add a comment that says this is a hack", code, "// this is a hack")
    eq(r["used_llm"], False, "unspoken '//' refused")
    r, _ = run_format("add a docstring", code, '"""Add a docstring."""')
    eq(r["used_llm"], False, "unspoken docstring refused")
    r, _ = run_format("hashtag todo fix this later", code, "# TODO fix this later")
    eq((r["text"], r["used_llm"]), ("# TODO fix this later", True), "'hashtag' allows '#'")
    r, _ = run_format("um, number sign include stdio", code, "#include stdio")
    eq(r["used_llm"], True, "'number sign' after a filler allows '#'")
    r, _ = run_format("slash slash fix me later", code, "// fix me later")
    eq(r["used_llm"], True, "'slash slash' allows '//'")
    r, _ = run_format("triple quote returns the user end triple quote", code, '"""Returns the user."""')
    eq(r["used_llm"], True, "'triple quote' allows a docstring")
    r, _ = run_format("add a comment that says this is a hack", code, "Add a comment that says this is a hack")
    eq(r["used_llm"], True, "plain words pass")


def test_retry():
    formatter.RETRY_DELAY_S = 0
    conn = httpx.ConnectError("refused")
    r, c = run_format("hello there", {"category": "other"}, "Hello there.", errors=[conn])
    eq((r["text"], r["used_llm"], len(c.calls)), ("Hello there.", True, 2), "one retry after a connection error")
    r, c = run_format("hello there", {"category": "other"}, "Hello there.", errors=[conn, conn])
    eq((r["text"], r["used_llm"], len(c.calls)), ("Hello there", False, 2), "second failure falls back to raw")
    r, c = run_format("hello there", {"category": "other"}, "Hello there.", errors=[httpx.ReadTimeout("slow")])
    eq((r["text"], r["used_llm"], len(c.calls)), ("Hello there", False, 1), "no retry on a timeout")


def test_messages():
    msgs = formatter._messages("chat", "Slack", "#eng", "", "hello")
    eq(msgs[0]["role"], "system", "system first")
    eq(msgs[-1]["role"], "user", "live request last")
    eq(len(msgs), 2 + 2 * len(formatter.FEWSHOT), "one user/assistant pair per few-shot example")
    roles = [m["role"] for m in msgs[1:-1]]
    eq(roles, ["user", "assistant"] * len(formatter.FEWSHOT), "few-shot roles alternate")
    eq("transcript:\n<<<\nhello\n>>>" in msgs[-1]["content"], True, "live transcript in last message")


def main():
    logging.disable(logging.WARNING)   # the fallback paths log on purpose
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
