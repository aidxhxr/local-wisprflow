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
p.add_argument("--no-live", action="store_true", default=os.environ.get("DICTATE_LIVE", "1") in ("0", "no", "false"),
               help="do not type the transcript live while speaking; insert only on release (or DICTATE_LIVE=0)")
p.add_argument("--fake-audio", help="(testing) stream this 16 kHz mono wav instead of the mic")
p.add_argument("--no-insert", action="store_true", help="(testing) log what would be typed/pasted, touch no app")
p.add_argument("--mic", default=os.environ.get("DICTATE_MIC", "builtin"),
               help="input device: 'builtin' (default: the Mac's own mic, so Bluetooth headphones keep playing "
                    "music), 'default' (system default input), or part of a device name (or DICTATE_MIC)")
p.add_argument("--debug", action="store_true")
args = p.parse_args()
HOTKEY = HOTKEYS[args.key]


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ====================================================================== SSH tunnel
def port_in_use_by_other_ssh() -> bool:
    try:
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{args.port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    except Exception:
        return False
    return any(line.startswith("ssh") and str(os.getpid()) not in line for line in out.splitlines()[1:])


class Tunnel:
    def __init__(self):
        self.proc, self.stop = None, False

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self.stop:
            # A client that just exited takes its tunnel down a moment later; wait for the port so
            # we do not fail the forward (or connect through a tunnel that is about to vanish).
            for _ in range(50):
                if not port_in_use_by_other_ssh():
                    break
                time.sleep(0.1)
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
    def listening(self, where, handsfree=False, live=False):
        self._show()
        self._dot(self.RED, True)
        head = "Hands-free · tap to stop" if handsfree else "Listening"
        self.title.setStringValue_(head + (" · live" if live else "") + " · " + where)
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


def frontmost_pid() -> int:
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()) if app is not None else 0


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
BUILTIN_MIC_HINTS = ("macbook", "built-in", "imac", "mac mini", "mac studio", "studio display")
BLUETOOTH_HINTS = ("airpods", "beats", "bluetooth", "buds", "wh-", "wf-", "bose", "jabra", "sony")


class Recorder:
    def __init__(self, sink):
        self.sink, self.stream = sink, None
        import sounddevice as sd
        self.sd = sd
        self.last_device = None

    def pick_device(self):
        """Which input to open. Opening a Bluetooth headset's mic flips it into the low-quality
        hands-free profile: music pauses, playback drops to mono, and the switch itself takes a
        second. The Mac's own mic is a better dictation mic anyway, so it is the default."""
        want = (args.mic or "builtin").strip().lower()
        if want == "default":
            return None
        devs = [(i, d) for i, d in enumerate(self.sd.query_devices()) if d["max_input_channels"] > 0]
        if want == "builtin":
            for i, d in devs:
                if any(h in d["name"].lower() for h in BUILTIN_MIC_HINTS):
                    return i
            # Any non-Bluetooth input beats a Bluetooth one.
            for i, d in devs:
                if not any(h in d["name"].lower() for h in BLUETOOTH_HINTS):
                    return i
            return None
        for i, d in devs:
            if want in d["name"].lower():
                return i
        log(f"  no input device matching {args.mic!r}; using the system default")
        return None

    def device_name(self, idx):
        try:
            return self.sd.query_devices(idx if idx is not None else self.sd.default.device[0])["name"]
        except Exception:
            return "default"

    def start(self):
        try:
            self._open()
        except Exception as e:
            # PortAudio caches the device list at init. When the default input changes (AirPods
            # connect, a headset is unplugged) opening the stale default fails with an internal
            # error. Re-scan devices and try once more before giving up.
            log(f"  mic open failed ({e.__class__.__name__}), rescanning audio devices")
            try:
                self.sd._terminate(); self.sd._initialize()
            except Exception:
                pass
            self._open()

    def _open(self):
        dev = self.pick_device()
        self.stream = self.sd.InputStream(device=dev, samplerate=SR, channels=1, dtype="int16", blocksize=1024,
                                          callback=lambda d, f, t, s: self.sink(d.tobytes()))
        self.stream.start()
        name = self.device_name(dev)
        if name != self.last_device:
            log(f"  mic: {name}")
            self.last_device = name

    def stop(self):
        if self.stream:
            self.stream.stop(); self.stream.close(); self.stream = None

    def check(self):
        dev = self.pick_device()
        self.sd.check_input_settings(device=dev, samplerate=SR, channels=1)
        log(f"microphone: {self.device_name(dev)}" + ("" if dev is not None else " (system default)"))


class FakeRecorder:
    """Streams a wav file at real-time pace (testing only); calls on_end when the clip is over."""
    def __init__(self, sink, path, on_end=None):
        self.sink, self.run, self.on_end = sink, False, on_end
        w = wave.open(path); assert w.getframerate() == SR and w.getnchannels() == 1
        self.pcm = w.readframes(w.getnframes())
        self.seconds = len(self.pcm) / 2 / SR

    def start(self):
        self.run = True
        def go():
            for i in range(0, len(self.pcm), 2048):
                if not self.run:
                    return
                self.sink(self.pcm[i:i + 2048]); time.sleep(0.064)
            if self.on_end:
                threading.Timer(0.4, self.on_end).start()
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
            ws, self.ws = self.ws, None
            try:
                if ws is not None:
                    ws.close()          # otherwise the dead socket sits in CLOSE_WAIT for the process lifetime
            except Exception:
                pass
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
    STABLE = 40     # chars before the live tail that partials may not rewrite; the final still can
    ANCHOR = 24     # chars of stable text used to re-align a revised transcript

    def __init__(self, kb):
        self.kb = kb
        self.text = ""

    def reset(self):
        self.text = ""

    def _backspace(self, n):
        if args.no_insert:
            if n:
                log(f"  [no-insert] backspace x{n}")
            return
        for _ in range(n):
            self.kb.tap(keyboard.Key.backspace)

    def _type(self, s):
        if not s:
            return
        if args.no_insert:
            log(f"  [no-insert] type {s!r}")
            return
        self.kb.type(s)

    def _splice_stable(self, new: str) -> str:
        """ASR re-transcribes the whole window, so it keeps changing 'No wait' to 'no wait' or a
        comma to a colon forty words back. Rewriting that drags the cursor across text the user
        has already read. Keep our stable head and take only the tail from the revision, aligned
        on the last few characters of the head. The formatted final fixes the head anyway."""
        head = self.text[:len(self.text) - self.STABLE]
        anchor = "".join(c.lower() for c in head[-self.ANCHOR:] if c.isalnum())
        if not anchor:
            return new
        # Align on letters and digits only, so the flip we are ignoring cannot break the anchor.
        pos = [k for k, c in enumerate(new) if c.isalnum()]
        norm = "".join(new[k].lower() for k in pos)
        start = max(0, sum(c.isalnum() for c in head) - len(anchor) - 40)
        j = norm.find(anchor, start)
        if j < 0:
            return new                      # cannot align: real rewrite
        tail = new[pos[j + len(anchor) - 1] + 1:]
        if head and not head[-1].isalnum() and not head[-1].isspace():
            tail = tail.lstrip(",.;:!?")    # head already carries its punctuation
        return head + tail

    def update(self, new: str):
        """Make the app show `new` in place of what we typed so far."""
        if new == self.text:
            return
        i = common_prefix_len(self.text, new)
        if i < len(self.text) - self.STABLE:
            new = self._splice_stable(new)
            i = common_prefix_len(self.text, new)
            if new == self.text:
                return
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
    if args.no_insert:
        log(f"  [no-insert] paste {text!r}")
        return
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
        self.rec = (FakeRecorder(self._audio, args.fake_audio, on_end=self.stop) if args.fake_audio
                    else Recorder(self._audio))
        self.lock = threading.Lock()
        self.active = False          # a session is recording
        self.handsfree = False
        self.held = False
        self.press_t = 0.0
        self.where = ""
        self.sid = ""
        self.t_stop = 0.0
        self.out_q: queue.Queue = queue.Queue()   # partials and finals, in order, one worker
        self.live = LiveText(kb)
        self.live_on = False         # typing partials into the app for this session
        self.live_pid = 0            # frontmost app when the session began
        self.live_seg = 1            # hands-free segment the live text belongs to
        self.live_seq = 0            # newest partial seq typed
        self.final_sid = ""          # id of the last session that produced a final
        self.finals_seen: set = set()   # ids of all sessions that produced a final
        threading.Thread(target=self._out_loop, daemon=True).start()

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
            try:
                self.rec.start()          # open the mic first: nothing else should change if it fails
            except Exception as e:
                log(f"  microphone unavailable: {e}")
                self.ov.error("Microphone unavailable: check the input device in System Settings → Sound")
                ding("Basso")
                return
            self.where = ctx["app"] or "?"
            self.sid = f"{time.time():.3f}"
            self.streamer.send({"type": "start", "id": self.sid, "mode": "hold", "context": ctx})
            self.live.reset()
            self.live_seg, self.live_seq = 1, 0
            self.live_pid = frontmost_pid()
            # With a selection the utterance is an instruction ("make this formal"); typing it
            # would destroy the selection the formatter is about to rewrite.
            self.live_on = not args.no_live and not ctx["selected_text"]
            self.active, self.handsfree = True, False
            ding("Tink")
            self.ov.listening(self.where, live=self.live_on)
            sel = f" | selection: {ctx['selected_text'][:40]!r}" if ctx["selected_text"] else ""
            log(f"▶ {self.where} ({ctx['bundle']}) {ctx['title'][:50]!r}{sel}")

    def go_handsfree(self):
        with self.lock:
            if not self.active:
                return
            self.handsfree = True
            self.streamer.send({"type": "mode", "mode": "auto"})
            self.ov.listening(self.where, handsfree=True, live=self.live_on)
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
        if sid not in self.finals_seen and not self.active:
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
            self.out_q.put(m)
        elif t == "started":
            self.where = f"{self.where} · {m.get('category')}"
            if self.active:
                self.ov.listening(self.where, self.handsfree, live=self.live_on)
        elif t == "final":
            self.out_q.put(m)

    def _out_loop(self):
        """Everything that touches the target app happens here, in message order: a partial can
        never be typed after the final that supersedes it."""
        while True:
            m = self.out_q.get()
            if m.get("type") == "partial":
                # Coalesce: if more partials are already waiting, only the newest matters.
                while not self.out_q.empty() and self.out_q.queue[0].get("type") == "partial":
                    m = self.out_q.get()
                self._on_partial(m)
                continue
            self._on_final(m)

    def _on_partial(self, m):
        if not self.live_on or m.get("id") != self.sid:
            return
        if not self._same_app():
            return
        seg, seq = m.get("segment", self.live_seg), m.get("seq")
        if seq is None:                 # older server without numbering: trust message order
            seq = self.live_seq + 1
        if seg < self.live_seg or (seg == self.live_seg and seq <= self.live_seq):
            return                      # older than what is already on screen
        if seg > self.live_seg:
            # Hands-free: the previous segment was finalised; what follows is fresh text.
            self.live.reset()
            self.live_seg = seg
        self.live_seq = seq
        # Parakeet guesses sentence-final punctuation on whatever the last word is right now;
        # the next partial usually takes it back. Hold it until the final.
        self.live.update((m.get("text") or "").rstrip(" .,;:"))

    def _same_app(self) -> bool:
        """If the user switched apps mid-dictation, stop typing: backspaces would hit the wrong
        window. The text already typed stays where it is; the final is pasted into the new app."""
        if self.live_on and frontmost_pid() != self.live_pid:
            log("  focus changed, live text off for this session")
            self.live_on = False
            self.live.reset()
        return self.live_on

    def _on_final(self, m):
        self.final_sid = m.get("id") or self.final_sid
        self.finals_seen.add(self.final_sid)
        self._same_app()
        if m.get("error"):
            log(f"  server error: {m['error']}")
            self.ov.error("Server error: " + m["error"]); ding("Basso")
            return
        text = (m.get("text") or "").strip()
        tm = m.get("timings") or {}
        lat = (time.time() - self.t_stop) * 1000 if self.t_stop and not self.handsfree else 0
        timing = f"asr {tm.get('asr_ms', 0)} ms · llm {tm.get('llm_ms', 0)} ms" + (f" · {lat:.0f} ms after release" if lat else "")
        if text:
            sep = "" if text.endswith("\n") else " "
            if self.live_on and m.get("id") == self.sid:
                self.live.replace(text + sep, insert_text)
            else:
                insert_text(text + sep)
        elif self.live_on and m.get("id") == self.sid:
            self.live.clear()
        if self.live_on and m.get("id") == self.sid:
            self.live_seg = (m.get("segment") or self.live_seg) + 1   # next partials start clean
        keep = self.active and self.handsfree
        self.ov.done(text, timing, keep=keep)
        if keep:
            self.ov.listening(self.where, True, live=self.live_on)
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
        if port_in_use_by_other_ssh():
            log(f"port {args.port} is held by another ssh process: a previous client's tunnel is still up. "
                f"Run: pkill -f 'ssh -N.*{args.port}'")
        os._exit(1)
    log("server ok. checking microphone ...")
    d.rec.check()
    listener = keyboard.Listener(on_press=d.on_press, on_release=d.on_release)
    listener.start()
    log(f"ready. hold [{args.key}] to dictate · tap for hands-free · select text + speak to edit · Ctrl-C quits")
    if args.fake_audio:
        # Smoke test: run one full session without the hotkey, then exit once the final lands.
        log(f"fake audio: streaming {args.fake_audio} ({d.rec.seconds:.1f}s), then exiting")
        threading.Timer(0.5, d.start).start()
        def watch():
            t0 = time.time()
            while time.time() - t0 < d.rec.seconds + FINAL_TIMEOUT_S + 2:
                if d.final_sid and d.final_sid == d.sid and not d.active:
                    time.sleep(1.5); os._exit(0)
                time.sleep(0.1)
            log("fake audio: no final received"); os._exit(1)
        threading.Thread(target=watch, daemon=True).start()

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
