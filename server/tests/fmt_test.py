import asyncio, os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from formatter import Formatter

CASES = [
    ("other", "TextEdit", "", "", "Um, so I was thinking that, uh, we could maybe ship the the new version on Monday, no, actually Wednesday, and then, like, do the announcement on Friday."),
    ("chat", "Slack", "#eng", "", "Yeah, sounds good, I'll take a look. Scratch that. I'm slammed today, can someone else take it?"),
    ("chat", "Messages", "", "", "Omw, be there in ten."),
    ("terminal", "Terminal", "zsh", "", "Docker compose up dash d dash dash build."),
    ("terminal", "Terminal", "zsh", "", "cd tilde slash documents slash local dash dictate and run the client."),
    ("ai_chat", "Terminal", "✳ Claude Code", "", "Can you refactor the websocket handler in server dot py so that finals are sent from a single sender task? Also add a unit test for the VAD class."),
    ("code", "Cursor", "client.py — local-dictate", "", "Add a comment that says this is a hack, we should use the accessibility API instead. New line. Fix me colon handle the timeout."),
    ("code", "Cursor", "", "", "Snake case get selected text."),
    ("email", "Mail", "", "", "Hi Sarah comma new paragraph Thanks for sending the draft period I've left a few comments inline comma mostly on section two period Could we chat tomorrow at two thirty question mark new paragraph Best comma Amir"),
    ("ai_chat", "Chrome", "ChatGPT", "", "Explain how speculative decoding works, and, um, write a short poem about it."),
    ("document", "Notes", "", "", "Groceries colon bullet oat milk bullet two dozen eggs bullet coffee period new paragraph Also call the dentist about the appointment on the fifteenth."),
    ("other", "Chrome", "Zillow", "", "The rent is twenty two hundred dollars a month, which is like fifteen percent more than last year. Email me at amir dot aidarkhan at gmail dot com."),
    ("chat", "Slack", "", "i cant come tmrw sorry smth came up", "Make this sound more polite and professional."),
    ("document", "Notes", "", "The quick brown fox jumps over the lazy dog. This sentence is often used because it contains every letter of the alphabet.", "Shorten this to one sentence."),
    ("document", "Notes", "", "some selected text", "Tomorrow's meeting is moved to three p.m."),
    ("other", "", "", "", "What's the capital of France?"),
    ("other", "", "", "", "Um. Uh, hmm."),
    ("terminal", "Terminal", "", "", "Okay, so, um, I want you to look through the entire codebase and find every place where we call the old API."),
    ("other", "", "", "", "Press the button three times, then wait for one minute and check the four lights. The first one should be green."),
    ("chat", "WhatsApp", "", "", "Lol no way. Okay okay I'll be there at seven thirty, save me a seat."),
]

async def main():
    f = Formatter()
    print("llm health:", await f.health())
    await f.format("warm up please", {"category": "other"})
    tot = 0
    for cat, app, title, sel, tr in CASES:
        t0 = time.time()
        r = await f.format(tr, {"category": cat, "app": app, "title": title, "selected_text": sel})
        ms = (time.time() - t0) * 1000; tot += ms
        print(f"\n[{cat}/{app}] {ms:.0f} ms llm={r['used_llm']}" + (f"\n  SEL: {sel!r}" if sel else ""))
        print(f"  IN : {tr}")
        print(f"  OUT: {r['text']!r}")
    print(f"\navg {tot/len(CASES):.0f} ms")

asyncio.run(main())
