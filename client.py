"""Push-to-talk dictation client for macOS with live preview and app-aware formatting.

Hold the hotkey (default Right Option) to dictate; release to insert.
Tap it once for hands-free mode (auto-segments on pauses); tap again to stop.
Select text first and speak an instruction ("make this more formal") to rewrite the selection.
"""
import argparse, atexit, json, os, queue, signal, subprocess, sys, threading, time, wave
import numpy as np
import websocket
from pynput import keyboard

import objc
from AppKit import (NSApplication, NSPanel, NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
                    NSBackingStoreBuffered, NSColor, NSTextField, NSView, NSFont, NSScreen, NSStatusWindowLevel,
                    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorStationary, NSApplicationActivationPolicyAccessory, NSMakeRect,
                    NSWorkspace, NSPasteboard, NSPasteboardTypeString, NSLineBreakByTruncatingHead,
                    NSFontWeightMedium, NSFontWeightRegular)
from Foundation import NSObject, NSTimer
from PyObjCTools import AppHelper
from Quartz import CABasicAnimation
import Quartz
from ApplicationServices import (AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
                                 AXUIElementSetMessagingTimeout, kAXFocusedUIElementAttribute,
                                 kAXSelectedTextAttribute, kAXTitleAttribute, kAXFocusedWindowAttribute)

SR = 16000
TAP_S = 0.35
FINAL_TIMEOUT_S = 12        # give up waiting for the server's final after this long
HOTKEYS = {"alt_r": keyboard.Key.alt_r, "alt_l": keyboard.Key.alt_l, "cmd_r": keyboard.Key.cmd_r,
           "ctrl_r": keyboard.Key.ctrl_r, "shift_r": keyboard.Key.shift_r,
           "f13": keyboard.Key.f13, "f18": keyboard.Key.f18, "f19": keyboard.Key.f19}

p = argparse.ArgumentParser()
p.add_argument("--host", default=os.environ.get("DICTATE_SSH"),
               help="ssh target of the GPU box, e.g. user@gpu-box (default: $DICTATE_SSH)")
p.add_argument("--port", type=int, default=8765)
p.add_argument("--key", default="alt_r", choices=sorted(HOTKEYS))
p.add_argument("--no-tunnel", action="store_true", help="assume the tunnel is already up")
p.add_argument("--quiet", action="store_true", help="no start/stop sounds")
p.add_argument("--no-overlay", action="store_true")
p.add_argument("--type", action="store_true", help="type characters instead of pasting")
p.add_argument("--fake-audio", help="(testing) stream this 16 kHz mono wav instead of the mic")
p.add_argument("--debug", action="store_true")
args = p.parse_args()
HOTKEY = HOTKEYS[args.key]


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ====================================================================== SSH tunnel
class Tunnel:
    def __init__(self):
        self.proc, self.stop = None, False

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop:
            ssh = (f"ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 "
                   f"-o BatchMode=yes -L {args.port}:127.0.0.1:{args.port} {args.host}")
            # Watchdog: if this client dies for any reason, the tunnel goes with it.
            script = (f"{ssh} & p=$!; trap 'kill $p 2>/dev/null; exit 0' TERM INT HUP; "
                      f"while kill -0 {os.getpid()} 2>/dev/null && kill -0 $p 2>/dev/null; do sleep 1; done; kill $p 2>/dev/null")
            self.proc = subprocess.Popen(["sh", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.proc.wait()
            if not self.stop:
                time.sleep(2)

    def close(self):
        self.stop = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ====================================================================== overlay (main thread only)
def on_main(fn):
    def wrapper(*a, **k):
        AppHelper.callAfter(fn, *a, **k)
    return wrapper


class OverlayPanel(NSPanel):
    # Never take keyboard focus away from the app the user is dictating into.
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class Overlay:
    W, H = 600, 74
    RED, BLUE, GREEN, GRAY = (0.95, 0.30, 0.30), (0.35, 0.60, 1.0), (0.35, 0.85, 0.50), (0.5, 0.5, 0.5)

    def __init__(self):
        self.panel = OverlayPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.W, self.H),
            NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel, NSBackingStoreBuffered, False)
        pnl = self.panel
        pnl.setLevel_(NSStatusWindowLevel)
        pnl.setOpaque_(False)
        pnl.setBackgroundColor_(NSColor.clearColor())
        pnl.setHasShadow_(True)
        pnl.setIgnoresMouseEvents_(True)
        pnl.setHidesOnDeactivate_(False)
        pnl.setBecomesKeyOnlyIfNeeded_(True)
        pnl.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces |
                                   NSWindowCollectionBehaviorFullScreenAuxiliary |
                                   NSWindowCollectionBehaviorStationary)
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, self.W, self.H))
        root.setWantsLayer_(True)
        root.layer().setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.09, 0.09, 0.10, 0.95).CGColor())
        root.layer().setCornerRadius_(18)
        root.layer().setBorderWidth_(1.0)
        root.layer().setBorderColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(1, 1, 1, 0.10).CGColor())
        pnl.setContentView_(root)

        self.dot = NSView.alloc().initWithFrame_(NSMakeRect(20, self.H - 29, 10, 10))
        self.dot.setWantsLayer_(True)
        self.dot.layer().setCornerRadius_(5)
        root.addSubview_(self.dot)

        self.title = self._label(NSMakeRect(38, self.H - 32, self.W - 56, 16), 12, NSFontWeightMedium, self.GRAY)
        self.text = self._label(NSMakeRect(20, 10, self.W - 40, 36), 15, NSFontWeightRegular, (1, 1, 1))
        self.text.setLineBreakMode_(NSLineBreakByTruncatingHead)
        self.text.setMaximumNumberOfLines_(2)
        self.text.cell().setWraps_(True)
        root.addSubview_(self.title)
        root.addSubview_(self.text)
        self._hide_timer = None
        self._place()

    def _label(self, rect, size, weight, rgb):
        tf = NSTextField.alloc().initWithFrame_(rect)
        tf.setBezeled_(False); tf.setDrawsBackground_(False); tf.setEditable_(False); tf.setSelectable_(False)
        tf.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
        tf.setTextColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, 1.0))
        return tf

    def _place(self):
        scr = NSScreen.mainScreen() or NSScreen.screens()[0]
        f = scr.visibleFrame()
        x = f.origin.x + (f.size.width - self.W) / 2
        y = f.origin.y + 56
        self.panel.setFrameOrigin_((x, y))

    def _dot(self, rgb, pulse):
        self.dot.layer().removeAllAnimations()
        self.dot.layer().setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(*rgb, 1.0).CGColor())
        self.dot.layer().setOpacity_(1.0)
        if pulse:
            a = CABasicAnimation.animationWithKeyPath_("opacity")
            a.setFromValue_(1.0); a.setToValue_(0.25); a.setDuration_(0.55)
            a.setAutoreverses_(True); a.setRepeatCount_(1e9)
            self.dot.layer().addAnimation_forKey_(a, "pulse")

    def _cancel_hide(self):
        if self._hide_timer:
            self._hide_timer.invalidate(); self._hide_timer = None

    def _show(self):
        self._cancel_hide()
        self._place()
        self.panel.orderFrontRegardless()

    @on_main
    def listening(self, where, handsfree=False):
        self._show()
        self._dot(self.RED, True)
        self.title.setStringValue_(("Hands-free · tap to stop · " if handsfree else "Listening · ") + where)
        self.text.setStringValue_("…")

    @on_main
    def partial(self, text):
        if text:
            self.text.setStringValue_(text)

    @on_main
    def processing(self, where, text):
        self._show()
        self._dot(self.BLUE, True)
        self.title.setStringValue_("Formatting · " + where)
        if text:
            self.text.setStringValue_(text)

    @on_main
    def done(self, text, timing="", keep=False):
        self._show()
        self._dot(self.GREEN, False)
        self.title.setStringValue_("Inserted" + (f" · {timing}" if timing else ""))
        self.text.setStringValue_(text or "(nothing heard)")
        if not keep:
            self._hide_later(1.3)

    @on_main
    def error(self, msg):
        self._show()
        self._dot(self.RED, False)
        self.title.setStringValue_("Error")
        self.text.setStringValue_(msg)
        self._hide_later(2.5)

    def _hide_later(self, secs):
        self._cancel_hide()
        self._hide_timer = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(secs, False, lambda t: self.panel.orderOut_(None))

    @on_main
    def hide(self):
        self._cancel_hide()
        self.panel.orderOut_(None)


class NullOverlay:
    def __getattr__(self, name):
        return lambda *a, **k: None


# ====================================================================== app context
def _cg_title(pid):
    try:
        wl = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
        for w in wl:
            if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0 and w.get("kCGWindowName"):
                return str(w["kCGWindowName"])
    except Exception:
        pass
    return ""


def get_context() -> dict:
    ctx = {"bundle": "", "app": "", "title": "", "selected_text": ""}
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return ctx
        pid = app.processIdentifier()
        ctx["bundle"] = str(app.bundleIdentifier() or "")
        ctx["app"] = str(app.localizedName() or "")
        el = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(el, 0.25)
        err, win = AXUIElementCopyAttributeValue(el, kAXFocusedWindowAttribute, None)
        if err == 0 and win is not None:
            err, title = AXUIElementCopyAttributeValue(win, kAXTitleAttribute, None)
            if err == 0 and title:
                ctx["title"] = str(title)
        if not ctx["title"]:
            ctx["title"] = _cg_title(pid)
        err, focused = AXUIElementCopyAttributeValue(el, kAXFocusedUIElementAttribute, None)
        if err == 0 and focused is not None:
            err, sel = AXUIElementCopyAttributeValue(focused, kAXSelectedTextAttribute, None)
            if err == 0 and sel and 0 < len(str(sel)) <= 4000:
                ctx["selected_text"] = str(sel)
    except Exception as e:
        log(f"context error: {e}")
    return ctx


# ====================================================================== audio
class Recorder:
    def __init__(self, sink):
        self.sink, self.stream = sink, None
        import sounddevice as sd
        self.sd = sd

    def start(self):
        self.stream = self.sd.InputStream(samplerate=SR, channels=1, dtype="int16", blocksize=1024,
                                          callback=lambda d, f, t, s: self.sink(d.tobytes()))
        self.stream.start()

    def stop(self):
        if self.stream:
            self.stream.stop(); self.stream.close(); self.stream = None

    def check(self):
        self.sd.check_input_settings(samplerate=SR, channels=1)


class FakeRecorder:
    """Streams a wav file at real-time pace (testing only)."""
    def __init__(self, sink, path):
        self.sink, self.run = sink, False
        w = wave.open(path); assert w.getframerate() == SR and w.getnchannels() == 1
        self.pcm = w.readframes(w.getnframes())

    def start(self):
        self.run = True
        def go():
            for i in range(0, len(self.pcm), 2048):
                if not self.run:
                    return
                self.sink(self.pcm[i:i + 2048]); time.sleep(0.064)
            while self.run:
                self.sink(bytes(2048)); time.sleep(0.064)   # silence after the clip
        threading.Thread(target=go, daemon=True).start()

    def stop(self):
        self.run = False

    def check(self):
        pass


# ====================================================================== streaming connection
class Streamer:
    def __init__(self, url, on_msg):
        self.url, self.on_msg = url, on_msg
        self.q: queue.Queue = queue.Queue()
        self.ws = None
        self.connected = threading.Event()
        threading.Thread(target=self._conn_loop, daemon=True).start()
        threading.Thread(target=self._send_loop, daemon=True).start()

    def _conn_loop(self):
        while True:
            try:
                ws = websocket.create_connection(self.url, timeout=10)
                ws.settimeout(None)
                self.ws = ws
                self.connected.set()
                while True:
                    m = ws.recv()
                    if not m:
                        break
                    self.on_msg(json.loads(m))
            except Exception as e:
                if self.connected.is_set():
                    log(f"stream disconnected: {e.__class__.__name__}")
            self.connected.clear()
            self.ws = None
            time.sleep(1)

    def _send_loop(self):
        while True:
            item = self.q.get()
            ws = self.ws
            if ws is None:
                continue   # drop; session is lost anyway
            try:
                if isinstance(item, bytes):
                    ws.send_binary(item)
                else:
                    ws.send(json.dumps(item))
            except Exception as e:
                log(f"send failed: {e.__class__.__name__}")

    def send(self, item):
        self.q.put(item)


# ====================================================================== output
kb = keyboard.Controller()


def common_prefix_len(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


class LiveText:
    """Mirror the streaming transcript at the cursor, Wispr Flow style.

    Keeps track of exactly what it has typed. Each partial is diffed against that: the diverging
    tail is backspaced and the new tail typed, so the committed part never flickers. When the
    formatted final arrives, `replace()` keeps whatever prefix already matches and only rewrites
    the rest, which for a short sentence is usually just the capitalisation and punctuation.
    """
    def __init__(self, kb):
        self.kb = kb
        self.text = ""

    def reset(self):
        self.text = ""

    def _backspace(self, n):
        for _ in range(n):
            self.kb.tap(keyboard.Key.backspace)

    def _type(self, s):
        if s:
            self.kb.type(s)

    def update(self, new: str):
        """Make the app show `new` in place of what we typed so far."""
        if new == self.text:
            return
        i = common_prefix_len(self.text, new)
        self._backspace(len(self.text) - i)
        self._type(new[i:])
        self.text = new

    def replace(self, final: str, insert):
        """Swap our live text for `final`; `insert(str)` is used for a long tail (paste)."""
        i = common_prefix_len(self.text, final)
        self._backspace(len(self.text) - i)
        tail = final[i:]
        if tail:
            if len(tail) <= 48 and "\n" not in tail:
                self._type(tail)
            else:
                insert(tail)
        self.text = ""

    def clear(self):
        self._backspace(len(self.text))
        self.text = ""


def insert_text(text: str):
    if args.type:
        kb.type(text)
        return
    pb = NSPasteboard.generalPasteboard()
    old = pb.stringForType_(NSPasteboardTypeString)
    pb.clearContents()
    ok = pb.setString_forType_(text, NSPasteboardTypeString)
    time.sleep(0.03)
    with kb.pressed(keyboard.Key.cmd):
        kb.press("v"); kb.release("v")
    if args.debug:
        fa = NSWorkspace.sharedWorkspace().frontmostApplication()
        log(f"  paste: set={ok} front={fa.localizedName() if fa else None} pb={pb.stringForType_(NSPasteboardTypeString)!r:.40}")
    time.sleep(0.3)
    if old is not None:
        pb.clearContents()
        pb.setString_forType_(old, NSPasteboardTypeString)


def ding(name):
    if not args.quiet:
        subprocess.Popen(["afplay", f"/System/Library/Sounds/{name}.aiff"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ====================================================================== controller
class Dictation:
    def __init__(self, overlay):
        self.ov = overlay
        self.streamer = Streamer(f"ws://127.0.0.1:{args.port}/stream", self.on_msg)
        self.rec = FakeRecorder(self._audio, args.fake_audio) if args.fake_audio else Recorder(self._audio)
        self.lock = threading.Lock()
        self.active = False          # a session is recording
        self.handsfree = False
        self.held = False
        self.press_t = 0.0
        self.where = ""
        self.sid = ""
        self.t_stop = 0.0
        self.paste_q: queue.Queue = queue.Queue()
        self.final_sid = ""          # id of the last session that produced a final
        threading.Thread(target=self._paste_loop, daemon=True).start()

    # -- audio sink (PortAudio thread)
    def _audio(self, raw: bytes):
        if self.active:
            self.streamer.send(raw)

    # -- session control
    def start(self):
        with self.lock:
            if self.active:
                return
            if not self.streamer.connected.is_set():
                self.ov.error("Not connected to the GPU box"); ding("Basso"); return
            ctx = get_context()
            self.where = ctx["app"] or "?"
            self.sid = f"{time.time():.3f}"
            self.streamer.send({"type": "start", "id": self.sid, "mode": "hold", "context": ctx})
            self.active, self.handsfree = True, False
            self.rec.start()
            ding("Tink")
            self.ov.listening(self.where)
            sel = f" | selection: {ctx['selected_text'][:40]!r}" if ctx["selected_text"] else ""
            log(f"▶ {self.where} ({ctx['bundle']}) {ctx['title'][:50]!r}{sel}")

    def go_handsfree(self):
        with self.lock:
            if not self.active:
                return
            self.handsfree = True
            self.streamer.send({"type": "mode", "mode": "auto"})
            self.ov.listening(self.where, handsfree=True)
            log("  hands-free on")

    def stop(self):
        with self.lock:
            if not self.active:
                return
            self.active = False
            self.rec.stop()
            self.t_stop = time.time()
            self.streamer.send({"type": "stop"})
            ding("Pop")
            self.ov.processing(self.where, "")
            threading.Timer(FINAL_TIMEOUT_S, self._check_final, args=[self.sid]).start()

    def _check_final(self, sid):
        """The server never answered our stop; don't leave the overlay stuck on 'Formatting'."""
        if self.final_sid != sid and not self.active:
            log(f"  no final from server after {FINAL_TIMEOUT_S}s (session {sid})")
            self.ov.error("No response from the GPU box. Check: journalctl --user -u dictate")
            ding("Basso")

    # -- hotkey (pynput thread)
    def on_press(self, key):
        if key != HOTKEY or self.held:
            return
        self.held, self.press_t = True, time.time()
        if not self.active:
            self.start()

    def on_release(self, key):
        if key != HOTKEY:
            return
        self.held = False
        if not self.active:
            return
        if self.handsfree:
            self.stop(); return
        if time.time() - self.press_t < TAP_S:
            self.go_handsfree(); return
        self.stop()

    # -- server messages (streamer thread)
    def on_msg(self, m):
        t = m.get("type")
        if t == "partial":
            self.ov.partial(m["text"])
        elif t == "started":
            self.where = f"{self.where} · {m.get('category')}"
            if self.active:
                self.ov.listening(self.where, self.handsfree)
        elif t == "final":
            self.paste_q.put(m)

    def _paste_loop(self):
        while True:
            m = self.paste_q.get()
            self.final_sid = m.get("id") or self.final_sid
            if m.get("error"):
                log(f"  server error: {m['error']}")
                self.ov.error("Server error: " + m["error"]); ding("Basso")
                continue
            text = (m.get("text") or "").strip()
            tm = m.get("timings") or {}
            lat = (time.time() - self.t_stop) * 1000 if self.t_stop and not self.handsfree else 0
            timing = f"asr {tm.get('asr_ms', 0)} ms · llm {tm.get('llm_ms', 0)} ms" + (f" · {lat:.0f} ms after release" if lat else "")
            if text:
                sep = "" if text.endswith("\n") else " "
                insert_text(text + sep)
            keep = self.active and self.handsfree
            self.ov.done(text, timing, keep=keep)
            if keep:
                self.ov.listening(self.where, True)
            log(f"  {m.get('audio_s', 0):4.1f}s | {timing} | {m.get('category')} | {m.get('raw')!r} -> {text!r}")


# ====================================================================== main
def main():
    tun = None
    if not args.no_tunnel:
        if not args.host:
            log("no GPU host: set DICTATE_SSH=user@gpu-box or pass --host (or --no-tunnel)")
            os._exit(2)
        tun = Tunnel(); tun.start()
        atexit.register(tun.close)
        log(f"opening tunnel to {args.host} ...")
    overlay = NullOverlay() if args.no_overlay else None
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    if overlay is None:
        overlay = Overlay()
    d = Dictation(overlay)
    if not d.streamer.connected.wait(30):
        log("server not reachable on the tunnel. Is dictate.service running on the GPU box?")
        os._exit(1)
    log("server ok. checking microphone ...")
    d.rec.check()
    listener = keyboard.Listener(on_press=d.on_press, on_release=d.on_release)
    listener.start()
    log(f"ready. hold [{args.key}] to dictate · tap for hands-free · select text + speak to edit · Ctrl-C quits")

    def quit_(*_):
        if tun:
            tun.close()
        os._exit(0)
    signal.signal(signal.SIGINT, quit_)
    signal.signal(signal.SIGTERM, quit_)
    # Periodic timer so Python signal handlers get a chance to run inside the Cocoa loop.
    NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.5, True, lambda t: None)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
