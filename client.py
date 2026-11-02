"""Push-to-talk dictation client for macOS with live preview and app-aware formatting.

Hold the hotkey (default Right Option) to dictate; release to insert.
Tap it once for hands-free mode (auto-segments on pauses); tap again to stop.
Select text first and speak an instruction ("make this more formal") to rewrite the selection.
"""
import argparse, atexit, fcntl, json, os, queue, signal, socket, subprocess, threading, time, wave
from collections import deque
import numpy as np
import websocket
from pynput import keyboard

from AppKit import (NSApplication, NSPanel, NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel,
                    NSBackingStoreBuffered, NSColor, NSTextField, NSView, NSFont, NSScreen, NSStatusWindowLevel,
                    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorStationary, NSApplicationActivationPolicyAccessory, NSMakeRect,
                    NSWorkspace, NSPasteboard, NSPasteboardTypeString, NSLineBreakByTruncatingHead,
                    NSFontWeightMedium, NSFontWeightRegular)
from Foundation import NSTimer
from PyObjCTools import AppHelper
from Quartz import CABasicAnimation
import Quartz
from ApplicationServices import (AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
                                 AXUIElementSetMessagingTimeout, kAXFocusedUIElementAttribute,
                                 kAXSelectedTextAttribute, kAXTitleAttribute, kAXFocusedWindowAttribute)

SR = 16000
TAP_S = 0.35
FINAL_TIMEOUT_S = 12        # give up waiting for the server's final after this long
FOCUS_CONFIRM_S = 0.15      # a focus change must still hold after this long to count
HANDSFREE_IDLE_S = 300      # hands-free stops itself after this long without a spoken segment
HOTKEYS = {"alt_r": keyboard.Key.alt_r, "alt_l": keyboard.Key.alt_l, "cmd_r": keyboard.Key.cmd_r,
           "ctrl_r": keyboard.Key.ctrl_r, "shift_r": keyboard.Key.shift_r,
           "f13": keyboard.Key.f13, "f18": keyboard.Key.f18, "f19": keyboard.Key.f19}

p = argparse.ArgumentParser()
p.add_argument("--host", default=os.environ.get("DICTATE_SSH"),
               help="ssh target of the GPU box, e.g. user@gpu-box (default: $DICTATE_SSH)")
p.add_argument("--port", type=int, default=int(os.environ.get("DICTATE_PORT", "8765")),
               help="local and remote port of the server (default: $DICTATE_PORT or 8765)")
p.add_argument("--key", default=os.environ.get("DICTATE_KEY", "alt_r"), choices=sorted(HOTKEYS),
               help="push-to-talk key (default: $DICTATE_KEY or alt_r)")
p.add_argument("--no-tunnel", action="store_true", help="assume the tunnel is already up")
p.add_argument("--quiet", action="store_true", help="no start/stop sounds")
p.add_argument("--no-overlay", action="store_true")
p.add_argument("--type", action="store_true", help="type characters instead of pasting")
p.add_argument("--paste-delay", type=float, default=float(os.environ.get("DICTATE_PASTE_DELAY", "0.3")), help="seconds between Cmd-V and restoring the clipboard; raise it if an app pastes your old clipboard instead (or DICTATE_PASTE_DELAY)")
p.add_argument("--no-live", action="store_true", default=os.environ.get("DICTATE_LIVE", "1") in ("0", "no", "false"),
               help="do not type the transcript live while speaking; insert only on release (or DICTATE_LIVE=0)")
p.add_argument("--fake-audio", help="(testing) stream this 16 kHz mono wav instead of the mic")
p.add_argument("--no-insert", action="store_true", help="(testing) log what would be typed/pasted, touch no app")
p.add_argument("--mic", default=os.environ.get("DICTATE_MIC", "default"),
               help="input device: 'default' (whatever macOS currently uses: AirPods when worn, etc.), "
                    "'builtin' (the Mac's own mic, keeps Bluetooth headphones in music mode), "
                    "or part of a device name (or DICTATE_MIC)")
p.add_argument("--debug", action="store_true")
p.add_argument("--list-mics", action="store_true", help="list input devices (the system default marked *) and exit")
p.add_argument("--doctor", action="store_true",
               help="check ssh, the server, the microphone and permissions; print PASS/FAIL per check and exit")
args = p.parse_args()
if args.key not in HOTKEYS:     # argparse does not check a default (DICTATE_KEY) against choices
    p.error(f"--key/DICTATE_KEY must be one of {', '.join(sorted(HOTKEYS))}, not {args.key!r}")
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


def port_listening(port: int, timeout=0.5) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=timeout).close()
        return True
    except OSError:
        return False


class Tunnel:
    def __init__(self):
        self.proc, self.stop = None, False
        self.stderr: deque = deque(maxlen=5)   # ssh's last lines: the reason it did not connect
        self.opens = 0

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def last_error(self) -> str:
        return self.stderr[-1] if self.stderr else ""

    def _loop(self):
        delay = 2
        while not self.stop:
            # A client that just exited takes its tunnel down a moment later; wait for the port so
            # we do not fail the forward (or connect through a tunnel that is about to vanish).
            for _ in range(50):
                if not port_in_use_by_other_ssh():
                    break
                time.sleep(0.1)
            ssh = (f"ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=3 "
                   f"-o ConnectTimeout=10 -o TCPKeepAlive=yes "
                   f"-o BatchMode=yes -L {args.port}:127.0.0.1:{args.port} {args.host}")
            # Watchdog: if this client dies for any reason, the tunnel goes with it.
            script = (f"{ssh} & p=$!; trap 'kill $p 2>/dev/null; exit 0' TERM INT HUP; "
                      f"while kill -0 {os.getpid()} 2>/dev/null && kill -0 $p 2>/dev/null; do sleep 1; done; kill $p 2>/dev/null")
            self.stderr.clear()
            t0 = time.time()
            self.proc = subprocess.Popen(["sh", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()
            opened = self._wait_open()
            self.proc.wait()
            if self.stop:
                break
            up = time.time() - t0
            # A tunnel that held for a while gets an immediate retry; one that keeps failing
            # (box down, auth broken) is retried at 2, 4, 8 ... 30 s instead of every 2 s.
            if opened and up > 30:
                delay = 2
            err = self.last_error()
            log(f"tunnel closed after {up:.0f}s" + (f": {err}" if err else "") + f"; retrying in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 30)

    def _read_stderr(self, proc):
        for line in proc.stderr:
            line = line.strip()
            if line and not line.startswith("Warning: Permanently added"):
                self.stderr.append(line)
                if args.debug:
                    log(f"  ssh: {line}")

    def _wait_open(self) -> bool:
        """Log once when the forward is listening: the only sign of life `ssh -N` gives."""
        while not self.stop and self.proc.poll() is None:
            if port_listening(args.port):
                self.opens += 1
                log("tunnel open" if self.opens == 1 else "tunnel reopened")
                return True
            time.sleep(0.25)
        return False

    def close(self):
        self.stop = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ====================================================================== overlay (main thread only)
from AppKit import NSEvent  # noqa: E402


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

    @staticmethod
    def _screen():
        """The screen the mouse is on. mainScreen() is the one with the key window, which on a
        multi-display setup is often not where the user is typing."""
        try:
            pt = NSEvent.mouseLocation()
            for scr in NSScreen.screens():
                f = scr.frame()
                if (f.origin.x <= pt.x < f.origin.x + f.size.width and
                        f.origin.y <= pt.y < f.origin.y + f.size.height):
                    return scr
        except Exception:
            pass
        return NSScreen.mainScreen() or NSScreen.screens()[0]

    def _place(self):
        f = self._screen().visibleFrame()
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
    def listening(self, where, handsfree=False, live=False, mic=""):
        self._show()
        self._dot(self.RED, True)
        head = "Hands-free · tap to stop" if handsfree else "Listening"
        if mic:
            where += " · mic: " + (mic if len(mic) <= 28 else mic[:27] + "…")
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
            # Long enough to read it back: a two-line result needs more than a word does.
            self._hide_later(min(1.3 + 0.02 * len(text or ""), 4.0))

    @on_main
    def notice(self, title, text="", hide_after=3.0):
        self._show()
        self._dot(self.GRAY, False)
        self.title.setStringValue_(title)
        self.text.setStringValue_(text)
        if hide_after:
            self._hide_later(hide_after)

    @on_main
    def cancelled(self):
        self._show()
        self._dot(self.GRAY, False)
        self.title.setStringValue_("Cancelled")
        self.text.setStringValue_("")
        self._hide_later(1.0)

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
from AppKit import NSApplicationActivationPolicyRegular  # noqa: E402


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


def frontmost_app():
    return NSWorkspace.sharedWorkspace().frontmostApplication()


def frontmost_pid() -> int:
    app = frontmost_app()
    return int(app.processIdentifier()) if app is not None else 0


def app_label(app) -> str:
    if app is None:
        return "?"
    return f"{app.localizedName()} ({app.bundleIdentifier() or app.processIdentifier()})"


def app_is_regular(app) -> bool:
    """Only ordinary windowed apps count as a focus target. Spotlight, Notification Center and
    menu-bar agents run as accessory or prohibited apps and borrow the foreground for a moment."""
    return app is not None and app.activationPolicy() == NSApplicationActivationPolicyRegular


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
SILENT_RMS = 0.0005     # below this a 1 s sample is digital silence, not a quiet room


class Recorder:
    def __init__(self, sink):
        self.sink, self.stream = sink, None
        import sounddevice as sd
        self.sd = sd
        self.last_device = None

    def pick_device(self):
        """Which input to open. Default: the system's current input, re-scanned every time (2 ms)
        so headphones connected since launch are seen. Note a Bluetooth headset's mic flips it into
        the hands-free profile while recording: music pauses and the switch takes about a second;
        --mic builtin avoids that."""
        want = (args.mic or "default").strip().lower()
        try:
            self.sd._terminate(); self.sd._initialize()   # PortAudio caches devices and the default
        except Exception:
            pass
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

    def measure(self, seconds=1.0):
        """Record `seconds` from the chosen device; returns (device name, rms in 0..1).
        Room tone on a working mic is ~0.002-0.01; a Bluetooth headset whose mic is not
        actually active delivers digital silence, ~0.0001 or exactly 0."""
        dev = self.pick_device()
        chunks = []
        s = self.sd.InputStream(device=dev, samplerate=SR, channels=1, dtype="int16", blocksize=1024,
                                callback=lambda d, f, t, st: chunks.append(d.copy()))
        s.start(); time.sleep(seconds); s.stop(); s.close()
        x = np.concatenate(chunks).astype(np.float32) / 32768 if chunks else np.zeros(1, np.float32)
        return self.device_name(dev), float(np.sqrt(np.mean(x * x)))

    def check(self):
        dev = self.pick_device()
        self.sd.check_input_settings(device=dev, samplerate=SR, channels=1)
        log(f"microphone: {self.device_name(dev)}" + ("" if dev is not None else " (system default)"))
        try:
            name, rms = self.measure(1.0)
        except Exception as e:
            log(f"  could not sample the microphone ({e.__class__.__name__}: {e}); continuing")
            return
        if rms < SILENT_RMS:
            log(f"WARNING: microphone {name!r} is delivering silence (rms {rms:.5f}). Dictation will come back "
                f"empty. If it is a Bluetooth headset, try --mic builtin; run --list-mics to see the inputs.")
        elif args.debug:
            log(f"  mic level ok (rms {rms:.4f})")


def list_input_devices() -> list:
    """[(index, name, is_default)] for every device with an input channel, freshly scanned."""
    import sounddevice as sd
    try:
        sd._terminate(); sd._initialize()
    except Exception:
        pass
    default = sd.default.device[0]
    return [(i, d["name"], i == default) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def print_input_devices():
    devs = list_input_devices()
    if not devs:
        print("no input devices found")
    for i, name, dflt in devs:
        print(f"{'*' if dflt else ' '} {i:2d}  {name}")
    print("* = system default input. Pick one with --mic 'part of name' or --mic builtin, or DICTATE_MIC.")


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
PING_S = 20     # idle ping interval; a tunnel that died quietly shows up here, not at the next hotkey press


class Streamer:
    def __init__(self, url, on_msg):
        self.url, self.on_msg = url, on_msg
        self.q: queue.Queue = queue.Queue()
        self.ws = None
        self.connected = threading.Event()
        self.connects = 0
        self.ping_t = self.pong_t = 0.0
        self.pongs = False           # this server answers pings (older ones ignore them)
        threading.Thread(target=self._conn_loop, daemon=True).start()
        threading.Thread(target=self._send_loop, daemon=True).start()

    def _conn_loop(self):
        while True:
            try:
                ws = websocket.create_connection(self.url, timeout=10)
                ws.settimeout(None)
                self.ws = ws
                self.connects += 1
                self.ping_t = self.pong_t = 0.0
                self.connected.set()
                if self.connects > 1:
                    log("stream reconnected")
                while True:
                    m = ws.recv()
                    if not m:
                        break
                    m = json.loads(m)
                    if m.get("type") == "pong":
                        self.pong_t, self.pongs = time.time(), True
                        continue
                    self.on_msg(m)
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
            try:
                item = self.q.get(timeout=PING_S)
            except queue.Empty:
                self._ping()
                continue
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

    def _ping(self):
        """Nothing was sent for PING_S. Ping the server; if the previous ping went unanswered the
        connection is dead even though the socket looks fine: close it so the receive loop
        reconnects now instead of the next session failing with 'Not connected'."""
        ws = self.ws
        if ws is None:
            return
        if self.pongs and self.ping_t and self.pong_t < self.ping_t:
            log(f"stream stalled: no answer to ping in {PING_S}s, reconnecting")
            try:
                ws.close()
            except Exception:
                pass
            return
        self.ping_t = time.time()
        try:
            ws.send(json.dumps({"type": "ping"}))
        except Exception as e:
            log(f"ping failed: {e.__class__.__name__}")

    def send(self, item):
        self.q.put(item)


# ====================================================================== output
kb = keyboard.Controller()
KEY_BURST = 24          # synthetic keystrokes sent back to back ...
KEY_PAUSE_S = 0.005     # ... before this pause, so a busy app's event queue keeps up


def type_keys(kb, s: str):
    """Type `s` in short bursts. pynput posts one event per character as fast as the loop runs;
    apps that handle keystrokes on a busy main thread (chat clients, editors with autocomplete)
    drop or reorder a long burst, and a dropped backspace leaves a stray character behind."""
    for k in range(0, len(s), KEY_BURST):
        if k:
            time.sleep(KEY_PAUSE_S)
        kb.type(s[k:k + KEY_BURST])


def untypable(s: str) -> bool:
    """pynput builds each key event with the character's Python length, which is one for an
    emoji that is two UTF-16 units, so anything outside the BMP comes out garbled or empty.
    Such text has to be pasted."""
    return any(ord(c) > 0xFFFF for c in s)


_warned: set = set()


def warn_once(key, msg):
    if key not in _warned:
        _warned.add(key)
        log(msg)


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

    def __init__(self, kb, paste=None):
        self.kb = kb
        self.paste = paste      # for text the keyboard cannot type; insert_text when None
        self.text = ""

    def reset(self):
        self.text = ""

    def _backspace(self, n):
        if args.no_insert:
            if n:
                log(f"  [no-insert] backspace x{n}")
            return
        for k in range(n):
            if k and k % KEY_BURST == 0:
                time.sleep(KEY_PAUSE_S)
            self.kb.tap(keyboard.Key.backspace)

    def _type(self, s):
        if not s:
            return
        if args.no_insert:
            log(f"  [no-insert] type {s!r}")
            return
        if untypable(s):
            warn_once("untypable", "  pasting instead of typing: the text has characters pynput cannot type")
            (self.paste or insert_text)(s)
            return
        type_keys(self.kb, s)

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
        # The anchor can occur more than once ("yeah yeah yeah", "no no no"); the first match
        # would splice the tail in too early and repeat words. Take the one nearest to where
        # the anchor sat in our own text.
        want = sum(c.isalnum() for c in head) - len(anchor)
        j, k = -1, norm.find(anchor, max(0, want - 40))
        while k >= 0 and (j < 0 or abs(k - want) < abs(j - want)):
            j, k = k, norm.find(anchor, k + 1)
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


def pasteboard_snapshot(pb) -> list:
    """Every item on the pasteboard with all of its types, so a copied image, file or rich
    text survives the paste; restoring the plain string alone lost those."""
    items = []
    for item in pb.pasteboardItems() or []:
        data = {}
        for t in item.types():
            if "promise" in t:      # reading a promised type would make the source app deliver it
                continue
            d = item.dataForType_(t)
            if d is not None:
                data[t] = d
        if data:
            items.append(data)
    return items


def pasteboard_restore(pb, items: list):
    from AppKit import NSPasteboardItem
    new = []
    for data in items:
        item = NSPasteboardItem.alloc().init()
        for t, d in data.items():
            item.setData_forType_(d, t)
        new.append(item)
    pb.clearContents()
    if new:
        pb.writeObjects_(new)


def insert_text(text: str):
    if args.no_insert:
        log(f"  [no-insert] paste {text!r}")
        return
    if args.type and not untypable(text):
        type_keys(kb, text)
        return
    if args.type:
        warn_once("untypable", "  pasting instead of typing: the text has characters pynput cannot type")
    pb = NSPasteboard.generalPasteboard()
    old = pasteboard_snapshot(pb)
    pb.clearContents()
    if not pb.setString_forType_(text, NSPasteboardTypeString):
        # Cmd-V now would paste whatever the clipboard held before. Type the text instead;
        # astral characters come out garbled, but nothing foreign lands in the document.
        warn_once("pasteboard", "  could not write the pasteboard; typing instead")
        if old:
            pasteboard_restore(pb, old)
        type_keys(kb, text)
        return
    mine = pb.changeCount()
    time.sleep(0.03)
    with kb.pressed(keyboard.Key.cmd):
        kb.press("v"); kb.release("v")
    if args.debug:
        fa = NSWorkspace.sharedWorkspace().frontmostApplication()
        log(f"  paste: front={fa.localizedName() if fa else None} pb={pb.stringForType_(NSPasteboardTypeString)!r:.40}")
    time.sleep(args.paste_delay)   # the app reads the pasteboard on its own schedule
    if pb.changeCount() != mine:
        return                  # the user copied something meanwhile; do not overwrite it
    if old:
        pasteboard_restore(pb, old)


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
        self.mic = ""                # input device of the current session, for the pill
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
        self.cancelled_sids: set = set()   # sessions the user abandoned with Escape; their finals are dropped
        self.last_text_t = 0.0       # hands-free: when the last segment with words came back
        self.idle_stopped = ""       # session the idle watchdog ended, to explain it when its final lands
        self._link_up = None         # last connection state the link watch saw; None until the first connect
        self._link_shown = False     # the pill is showing "Reconnecting"
        threading.Thread(target=self._out_loop, daemon=True).start()
        AppHelper.callAfter(self._watch_link)      # NSTimer must be created on the main thread

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
            self.mic = getattr(self.rec, "last_device", None) or ""
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
            self.ov.listening(self.where, live=self.live_on, mic=self.mic)
            sel = f" | selection: {ctx['selected_text'][:40]!r}" if ctx["selected_text"] else ""
            log(f"▶ {self.where} ({ctx['bundle']}) {ctx['title'][:50]!r}{sel}")

    def go_handsfree(self):
        with self.lock:
            if not self.active:
                return
            self.handsfree = True
            self.last_text_t = time.time()
            self.streamer.send({"type": "mode", "mode": "auto"})
            self.ov.listening(self.where, handsfree=True, live=self.live_on, mic=self.mic)
            log("  hands-free on")
            threading.Timer(15, self._handsfree_watch, args=[self.sid]).start()

    def _handsfree_watch(self, sid):
        """A forgotten hands-free session keeps the GPU busy and the microphone open. End it
        after HANDSFREE_IDLE_S without a segment that contained words."""
        if not (self.active and self.handsfree and self.sid == sid):
            return
        if time.time() - self.last_text_t < HANDSFREE_IDLE_S:
            threading.Timer(15, self._handsfree_watch, args=[sid]).start()
            return
        log(f"  hands-free: nothing heard for {HANDSFREE_IDLE_S // 60} min, stopping")
        self.idle_stopped = sid
        self.stop()

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

    def cancel(self):
        """Escape: throw the session away. Nothing is inserted, the live text is erased."""
        with self.lock:
            if not self.active:
                return
            self.active, self.handsfree = False, False
            self.rec.stop()
            self.cancelled_sids.add(self.sid)
            self.streamer.send({"type": "cancel"})
            # Erase the live text on the output thread, after any partial already queued.
            self.out_q.put({"type": "cancel", "id": self.sid})
            ding("Pop")
            self.ov.cancelled()
            log(f"  cancelled (session {self.sid})")

    def _check_final(self, sid):
        """The server never answered our stop; don't leave the overlay stuck on 'Formatting'."""
        if sid not in self.finals_seen and not self.active:
            log(f"  no final from server after {FINAL_TIMEOUT_S}s (session {sid})")
            self.ov.error("No response from the GPU box. Check: journalctl --user -u dictate")
            ding("Basso")

    # -- connection watch (main thread)
    def _watch_link(self):
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(2.0, True, self._link_tick)

    def _link_tick(self, _timer=None):
        """Say so in the pill when the stream drops while idle, otherwise the first sign of a
        dead tunnel is a hotkey press that does nothing. main() reports the initial connect."""
        up = self.streamer.connected.is_set()
        if self._link_up is None:
            self._link_up = True if up else None
            return
        if up and not self._link_up:
            log("stream reconnected")
            if self._link_shown:
                self.ov.hide(); self._link_shown = False
        elif not up and not self.active:
            self.ov.notice("Reconnecting to the GPU box…", "Dictation is off until the stream is back", hide_after=0)
            self._link_shown = True
        self._link_up = up

    # -- hotkey (pynput thread)
    def on_press(self, key):
        if key == keyboard.Key.esc:
            if self.active:
                self.cancel()
            return
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
            if m.get("id") != self.sid:
                return                  # late answer to a session a quick re-press already replaced
            self.where = f"{self.where} · {m.get('category')}"
            if self.active:
                self.ov.listening(self.where, self.handsfree, live=self.live_on, mic=self.mic)
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
            if m.get("type") == "cancel":
                if self.live_on and m.get("id") == self.sid:
                    self.live.clear()
                self.live_on = False
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
        window. The text already typed stays where it is; the final is pasted into the new app.

        A single sample of the frontmost app is not evidence of a switch: Spotlight, a
        notification or a menu-bar agent takes the foreground for a moment and gives it back,
        and the frontmost app is briefly ambiguous while macOS activates a window. Only give up
        when a regular app other than ours is in front on two samples FOCUS_CONFIRM_S apart."""
        if not self.live_on:
            return False
        app = frontmost_app()
        if app is None or int(app.processIdentifier()) == self.live_pid:
            return True
        if not app_is_regular(app):
            if args.debug:
                log(f"  frontmost is {app_label(app)}, a transient app; keeping live text")
            return True
        time.sleep(FOCUS_CONFIRM_S)
        again = frontmost_app()
        if again is None or int(again.processIdentifier()) == self.live_pid or not app_is_regular(again):
            log(f"  focus flickered to {app_label(app)} and back; keeping live text")
            return True
        log(f"  focus changed to {app_label(again)}, live text off for this session")
        self.live_on = False
        self.live.reset()
        return False

    def _on_final(self, m):
        if m.get("id") in self.cancelled_sids:
            log(f"  dropping final of cancelled session {m.get('id')}: {m.get('raw')!r}")
            return
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
        if text:
            self.last_text_t = time.time()
        keep = self.active and self.handsfree
        if not text and not keep and (m.get("audio_s") or 0) > 2:
            # Seconds of audio and not a word: a silent input (a Bluetooth headset in the wrong
            # profile, a muted interface) is the usual reason.
            self.ov.notice(f"Nothing heard in {m['audio_s']:.0f} s · {timing}",
                           "Check the microphone" + (f": {self.mic}" if self.mic else ""))
        else:
            self.ov.done(text, timing, keep=keep)
        if keep:
            self.ov.listening(self.where, True, live=self.live_on, mic=self.mic)
        elif m.get("id") == self.idle_stopped:
            self.ov.notice(f"Hands-free off: nothing heard for {HANDSFREE_IDLE_S // 60} min", text)
        log(f"  {m.get('audio_s', 0):4.1f}s | {timing} | {m.get('category')} | {m.get('raw')!r} -> {text!r}")


# ====================================================================== single instance
LOCK_DIR = os.path.expanduser("~/Library/Application Support/local-wisprflow")
LOCK_PATH = os.path.join(LOCK_DIR, "client.lock")
_lock_fd = None     # held open for the process lifetime; the kernel drops the flock when we die


def lock_holder_pid() -> int:
    """pid written in the lock file if another process holds the lock, else 0."""
    try:
        fd = os.open(LOCK_PATH, os.O_RDONLY)
    except OSError:
        return 0
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return 0
    except OSError:
        try:
            return int(os.read(fd, 32).strip() or 0)
        except (OSError, ValueError):
            return -1
    finally:
        os.close(fd)


def acquire_instance_lock() -> bool:
    global _lock_fd
    os.makedirs(LOCK_DIR, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
    _lock_fd = fd
    return True


def other_client_pids() -> list:
    """Other `python client.py` processes (smoke tests with --fake-audio excluded): clients
    started before the lock file existed, or from another checkout."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    except Exception:
        return []
    pids = []
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        argv = cmd.split()
        if (len(argv) >= 2 and os.path.basename(argv[0]).startswith("python") and argv[1].endswith("client.py")
                and "--fake-audio" not in argv and "--doctor" not in argv and int(pid) != os.getpid()):
            pids.append(int(pid))
    return pids


# ====================================================================== doctor
def _ssh_run(cmd: str, timeout=20):
    """One-shot ssh to the GPU box with the tunnel's options; (returncode, stdout, last stderr line)."""
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.host, cmd],
                       capture_output=True, text=True, timeout=timeout)
    err = [ln for ln in r.stderr.splitlines() if ln.strip() and not ln.startswith("Warning: Permanently added")]
    return r.returncode, r.stdout.strip(), (err[-1] if err else "")


def doctor() -> int:
    """Every failure this project has had, checked in one go. Returns the exit code."""
    failed = []

    def report(status, name, detail):
        print(f"{status:<5} {name:<17} {detail}", flush=True)
        if status == "FAIL":
            failed.append(name)

    term = os.environ.get("TERM_PROGRAM", "the terminal app you launch from")
    perm_hint = f"System Settings → Privacy & Security, grant {term}, then quit and reopen it"

    # 1. Is a client running at all? The most common "it's not working".
    pids = sorted(set(other_client_pids()) | ({lock_holder_pid()} - {0, -1}))
    if pids:
        report("PASS", "client", f"running (pid {', '.join(map(str, pids))})")
    else:
        report("WARN", "client", "no client is running; nothing listens for the hotkey. Start ./dictate")

    # 2. ssh key auth, the way the tunnel does it (BatchMode: no passphrase prompt possible).
    if not args.host:
        report("FAIL", "ssh", "no GPU host: set DICTATE_SSH=user@gpu-box or pass --host")
        ssh_ok = False
    else:
        try:
            rc, _, err = _ssh_run("true")
            ssh_ok = rc == 0
            report("PASS" if ssh_ok else "FAIL", "ssh",
                   f"{args.host} answers" if ssh_ok else f"{err or f'ssh exited {rc}'}. "
                   f"Fix: ssh -o BatchMode=yes {args.host} true (a passphrase key must be in ssh-agent)")
        except subprocess.TimeoutExpired:
            ssh_ok = False
            report("FAIL", "ssh", f"no answer from {args.host} in 20 s")

    # 3. The server on the box, asked on the box itself so it works whether or not a tunnel is up.
    if ssh_ok:
        try:
            rc, out, err = _ssh_run(f"curl -s -m 5 localhost:{args.port}/health")
            if rc == 0 and '"ok":true' in out.replace(" ", ""):
                report("PASS", "server", f"/health on the box: {out[:120]}")
            else:
                report("FAIL", "server", (f"/health on the box answered {out[:120]!r}" if out else
                                          f"nothing listens on localhost:{args.port} on the box ({err or f'curl exited {rc}'})")
                       + f". Fix: ssh {args.host} systemctl --user status dictate dictate-llm")
        except subprocess.TimeoutExpired:
            report("FAIL", "server", "the health probe on the box timed out")
    else:
        report("SKIP", "server", "ssh failed")

    # 4. The local tunnel port, if a client is up.
    if port_listening(args.port):
        try:
            import urllib.request
            body = urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=5).read().decode()
            report("PASS" if '"ok":true' in body.replace(" ", "") else "FAIL", "tunnel", f"port {args.port}: {body[:120]}")
        except Exception as e:
            report("FAIL", "tunnel", f"port {args.port} is open but /health failed: {e.__class__.__name__}: {e}")
    else:
        report("INFO", "tunnel", f"nothing on local port {args.port} (expected while no client runs)")

    # 5. Microphones and the level of the one we would record from.
    devs = list_input_devices()
    if not devs:
        report("FAIL", "microphones", "no input devices")
    else:
        report("PASS", "microphones", "; ".join(f"[{i}] {n}{' *default' if d else ''}" for i, n, d in devs))
        try:
            rec = Recorder(lambda b: None)
            dev = rec.pick_device()
            rec.sd.check_input_settings(device=dev, samplerate=SR, channels=1)
            name, rms = rec.measure(1.5)
            if rms < SILENT_RMS:
                report("FAIL", "mic level", f"{name!r} (--mic {args.mic}) delivered silence over 1.5 s (rms {rms:.5f}). "
                       f"A Bluetooth headset whose mic is not active does this: try --mic builtin. "
                       f"Or Microphone permission is missing: {perm_hint}")
            elif rms < 3 * SILENT_RMS:
                report("WARN", "mic level", f"{name!r} (--mic {args.mic}) rms {rms:.4f} over 1.5 s: very low. "
                       f"If dictation comes back empty, try --mic builtin")
            else:
                report("PASS", "mic level", f"{name!r} (--mic {args.mic}) rms {rms:.4f} over 1.5 s")
        except Exception as e:
            report("FAIL", "mic level", f"cannot record from --mic {args.mic}: {e.__class__.__name__}: {e}")

    # 6. The two permissions the hotkey and the paste need. Both are per launching app.
    try:
        from ApplicationServices import AXIsProcessTrusted
        ok = bool(AXIsProcessTrusted())
        report("PASS" if ok else "FAIL", "accessibility", "granted" if ok else f"not granted: {perm_hint} (Accessibility)")
    except Exception as e:
        report("SKIP", "accessibility", f"cannot query: {e.__class__.__name__}")
    try:
        ok = bool(Quartz.CGPreflightListenEventAccess())
        report("PASS" if ok else "FAIL", "input monitoring", "granted" if ok else f"not granted: {perm_hint} (Input Monitoring)")
    except AttributeError:
        report("SKIP", "input monitoring", "CGPreflightListenEventAccess not available in this pyobjc; check it by hand")
    except Exception as e:
        report("SKIP", "input monitoring", f"cannot query: {e.__class__.__name__}")

    print(("all checks passed" if not failed else f"FAILED: {', '.join(failed)}"), flush=True)
    return 1 if failed else 0


# ====================================================================== main
def main():
    if args.list_mics:
        print_input_devices()
        return
    if args.doctor:
        os._exit(doctor())
    if not args.fake_audio:     # smoke tests may run next to a live client (they use its tunnel)
        others = other_client_pids()
        locked = acquire_instance_lock()
        if not locked:
            others = sorted(set(others) | ({lock_holder_pid()} - {0, -1}))
        if others or not locked:
            who = f"pid {', '.join(map(str, others))}" if others else f"it holds {LOCK_PATH}"
            log(f"another client is already running ({who}); it owns the hotkey and the tunnel. "
                + (f"Quit it first: kill {' '.join(map(str, others))}" if others else "Quit it first."))
            os._exit(3)
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
    if not d.streamer.connected.wait(10):
        log("still waiting for the tunnel ..." if tun else f"still waiting for the server on port {args.port} ...")
        if not d.streamer.connected.wait(20):
            log("server not reachable on the tunnel. Is dictate.service running on the GPU box?")
            if tun:
                if tun.opens and tun.alive:
                    log(f"the tunnel is up; the box answered nothing on port {args.port}. "
                        f"Run: ssh {args.host} systemctl --user status dictate")
                else:
                    err = tun.last_error()
                    log("the ssh tunnel is not up" + (": " + err if err else " and ssh printed nothing")
                        + f". Run: ssh -o BatchMode=yes {args.host} true")
            if port_in_use_by_other_ssh() and not (tun and tun.alive):
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
