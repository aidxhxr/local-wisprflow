"""Dictation server: streaming Parakeet ASR + LLM formatting on one GPU.

WS   /stream       persistent connection; one utterance session at a time
POST /transcribe   raw int16 PCM -> text (batch, no LLM)
POST /format       {"text":..., "context":{...}} -> formatted text (LLM only)
GET  /health
"""
import asyncio, json, logging, os, time, threading
import numpy as np
import torch
from fastapi import FastAPI, Request, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from formatter import Formatter, clean, categorize
from vad import VAD

MODEL_NAME = os.environ.get("DICTATE_MODEL", "nvidia/parakeet-tdt-0.6b-v2")
HOST = os.environ.get("DICTATE_HOST", "127.0.0.1")
PORT = int(os.environ.get("DICTATE_PORT", "8765"))
SR = 16000
PARTIAL_MIN_INTERVAL = 0.22      # s between partial transcriptions
PARTIAL_MIN_NEW_AUDIO = 0.25     # s of new audio before re-transcribing
AUTO_SILENCE_MS = 850            # hands-free: finalize after this much trailing silence
AUTO_MIN_SPEECH_MS = 250         # ...but only if we heard at least this much speech
AUTO_MAX_SEGMENT_S = 40          # hands-free: force a cut at the next short pause past this
MAX_BUFFER_S = 600

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("dictate")
logging.getLogger("nemo_logger").setLevel(logging.ERROR)

app = FastAPI()
_gpu_lock = threading.Lock()
_model = None
_fmt = Formatter()
_vad = None            # shared, warmed at startup; sessions are sequential for one user
_vad_lock = threading.Lock()


# ------------------------------------------------------------------ ASR
def load_model():
    global _model
    import nemo.collections.asr as nemo_asr
    t0 = time.time()
    m = nemo_asr.models.ASRModel.from_pretrained(MODEL_NAME, map_location="cuda")
    m.eval()
    _asr(m, np.zeros(SR, dtype=np.float32))
    _model = m
    global _vad
    _vad = VAD()
    _vad.feed(np.zeros(SR * 2, dtype=np.float32))   # warm the JIT
    _vad.reset()
    log.info("model %s ready in %.1fs", MODEL_NAME, time.time() - t0)


def _is_fatal_cuda(e: BaseException) -> bool:
    """A sticky CUDA fault (illegal memory access, device-side assert, ...) poisons the whole
    context: every later kernel launch fails until the process restarts."""
    s = str(e)
    return e.__class__.__name__ == "AcceleratorError" or ("CUDA error" in s and "out of memory" not in s)


def _die(e: BaseException):
    log.critical("fatal CUDA error, exiting so systemd restarts the server: %s", e)
    logging.shutdown()
    os._exit(3)


def _probe_cuda():
    """Run a tiny inference to prove the context still works. An out-of-memory error can leave
    cuDNN in a state where the next real kernel faults with an illegal memory access; better to
    find out now and restart than to serve a dead model behind a green /health."""
    try:
        with _gpu_lock, torch.inference_mode():
            _model.transcribe([np.zeros(SR // 2, dtype=np.float32)], batch_size=1, verbose=False)
            torch.cuda.synchronize()
    except Exception as e:
        _die(e)


def _asr(m, audio: np.ndarray) -> str:
    try:
        with _gpu_lock, torch.inference_mode():
            out = m.transcribe([audio], batch_size=1, verbose=False)
    except torch.OutOfMemoryError:
        torch.cuda.empty_cache()
        _probe_cuda()
        raise
    except Exception as e:
        if _is_fatal_cuda(e):
            _die(e)
        raise
    h = out[0]
    if isinstance(h, (list, tuple)):
        h = h[0]
    text = h.text if hasattr(h, "text") else (h if isinstance(h, str) else "")
    return (text or "").strip()


def pcm16_to_f32(raw: bytes) -> np.ndarray:
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


def _resample(x: np.ndarray, sr: int) -> np.ndarray:
    if sr == SR:
        return x
    n = int(round(len(x) * SR / sr))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


# ------------------------------------------------------------------ HTTP
_last_probe = 0.0
PROBE_INTERVAL_S = 60


@app.get("/health")
async def health():
    """Green only if the model actually runs: at most once a minute, push silence through it."""
    global _last_probe
    if _model is not None and time.time() - _last_probe > PROBE_INTERVAL_S:
        _last_probe = time.time()
        await asyncio.to_thread(_probe_cuda)
    return {"ok": _model is not None, "model": MODEL_NAME, "llm": await _fmt.health()}


@app.post("/transcribe")
async def transcribe(request: Request, sr: int = Query(SR)):
    if _model is None:
        return JSONResponse({"error": "model loading"}, status_code=503)
    body = await request.body()
    if len(body) > MAX_BUFFER_S * sr * 2:
        return JSONResponse({"error": f"audio longer than {MAX_BUFFER_S}s"}, status_code=413)
    pcm = _resample(pcm16_to_f32(body), sr)
    audio_s = len(pcm) / SR
    if audio_s < 0.25:
        return {"text": "", "audio_s": audio_s, "infer_ms": 0.0}
    t0 = time.time()
    text = clean(await asyncio.to_thread(_asr, _model, pcm))
    ms = (time.time() - t0) * 1000
    log.info("%.2fs audio -> %.0f ms: %r", audio_s, ms, text)
    return {"text": text, "audio_s": audio_s, "infer_ms": ms}


@app.post("/format")
async def format_only(request: Request):
    body = await request.json()
    return await _fmt.format(body.get("text", ""), body.get("context") or {})


# ------------------------------------------------------------------ streaming session
class Session:
    def __init__(self, send, data: dict):
        self.send = send                      # async fn(dict)
        self.id = data.get("id") or f"{time.time():.3f}"
        self.mode = data.get("mode", "hold")  # hold | auto
        self.context = data.get("context") or {}
        self.context.setdefault("category", categorize(
            self.context.get("bundle", ""), self.context.get("app", ""), self.context.get("title", "")))
        self.chunks: list[np.ndarray] = []
        self.n = 0                            # samples in buffer
        self.n_transcribed = 0                # samples covered by self.partial_text
        self.partial_text = ""
        self.t_start = time.time()
        self.t_last_partial = 0.0
        self.closed = False
        self.vad = None
        self.vad_pending: list[np.ndarray] = []
        self.task = asyncio.create_task(self._loop())
        self.seg = 0

    # -- audio in (called from ws handler)
    def add_audio(self, raw: bytes):
        if self.closed or self.n > MAX_BUFFER_S * SR:
            return
        x = pcm16_to_f32(raw)
        self.chunks.append(x)
        self.n += len(x)
        if self.mode == "auto":
            self.vad_pending.append(x)

    def set_mode(self, mode: str):
        self.mode = mode
        if mode == "auto" and self.vad is None:
            self.vad = _vad
            with _vad_lock:
                self.vad.reset()
            self.vad_pending = list(self.chunks)   # catch up on what we already have

    def _buffer(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)

    # -- partial loop
    async def _loop(self):
        try:
            while not self.closed:
                await asyncio.sleep(0.03)
                if self.mode == "auto" and self.vad is not None and self.vad_pending:
                    pend, self.vad_pending = self.vad_pending, []
                    await asyncio.to_thread(self._vad_feed, pend)
                    if self._auto_should_cut():
                        await self._finalize_segment()
                        continue
                new_s = (self.n - self.n_transcribed) / SR
                if new_s >= PARTIAL_MIN_NEW_AUDIO and time.time() - self.t_last_partial >= PARTIAL_MIN_INTERVAL:
                    try:
                        await self._partial()
                    except Exception as e:
                        # One bad partial (OOM, transient CUDA hiccup) must not end live text for
                        # the rest of the utterance; the final still gets the whole buffer.
                        log.warning("partial failed, skipping: %s: %s", e.__class__.__name__, str(e)[:120])
                        self.t_last_partial = time.time()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("session loop")

    async def _partial(self):
        n = self.n
        buf = self._buffer()
        if len(buf) < SR * 0.3:
            return
        t0 = time.time()
        text = await asyncio.to_thread(_asr, _model, buf)
        self.t_last_partial = time.time()
        self.n_transcribed = n
        self.partial_text = text
        if not self.closed:
            await self.send({"type": "partial", "id": self.id, "text": clean(text),
                             "audio_s": round(n / SR, 2), "asr_ms": round((self.t_last_partial - t0) * 1000)})

    def _vad_feed(self, chunks):
        with _vad_lock:
            for x in chunks:
                self.vad.feed(x)

    def _auto_should_cut(self) -> bool:
        v = self.vad
        if v.speech_ms < AUTO_MIN_SPEECH_MS:
            return False
        if v.silence_ms >= AUTO_SILENCE_MS:
            return True
        return self.n / SR > AUTO_MAX_SEGMENT_S and v.silence_ms >= 350

    async def _finalize_segment(self):
        """Hands-free: emit a final for the current buffer and start a fresh one."""
        buf = self._buffer()
        self.chunks, self.n, self.n_transcribed, self.partial_text = [], 0, 0, ""
        with _vad_lock:
            self.vad.reset()
        self.seg += 1
        await self._emit_final(buf, reuse=None, segment=self.seg)

    async def _emit_final(self, buf: np.ndarray, reuse: str | None, segment: int):
        try:
            await self._emit_final_inner(buf, reuse, segment)
        except Exception as e:
            log.exception("final failed")
            await self.send({"type": "final", "id": self.id, "segment": segment, "text": "", "raw": "",
                             "audio_s": round(len(buf) / SR, 2), "error": f"{e.__class__.__name__}: {e}"[:200]})

    async def _emit_final_inner(self, buf: np.ndarray, reuse: str | None, segment: int):
        t0 = time.time()
        if len(buf) < SR * 0.3:
            await self.send({"type": "final", "id": self.id, "segment": segment, "text": "", "raw": "",
                             "audio_s": round(len(buf) / SR, 2)})
            return
        if reuse is not None:
            raw_text, asr_ms = reuse, 0.0
        else:
            raw_text = await asyncio.to_thread(_asr, _model, buf)
            asr_ms = (time.time() - t0) * 1000
        t1 = time.time()
        fmt = await _fmt.format(raw_text, self.context)
        total = (time.time() - t0) * 1000
        log.info("[%s] %.1fs audio | asr %.0f ms | llm %.0f ms%s | %s | %r -> %r",
                 self.context.get("category"), len(buf) / SR, asr_ms, fmt["llm_ms"],
                 "" if fmt["used_llm"] else " (fallback)", self.context.get("app", "?"), fmt["raw"], fmt["text"])
        await self.send({"type": "final", "id": self.id, "segment": segment, "text": fmt["text"],
                         "raw": fmt["raw"], "used_llm": fmt["used_llm"], "category": fmt["category"],
                         "audio_s": round(len(buf) / SR, 2),
                         "timings": {"asr_ms": round(asr_ms), "llm_ms": round(fmt["llm_ms"]), "server_ms": round(total)}})

    # -- end of session
    async def stop(self):
        self.closed = True
        self.task.cancel()
        try:
            await self.task
        except BaseException:
            pass
        buf = self._buffer()
        reuse = self.partial_text if (self.n_transcribed == self.n and self.n > 0) else None
        await self._emit_final(buf, reuse=reuse, segment=self.seg + 1)

    def cancel(self):
        self.closed = True
        self.task.cancel()


@app.websocket("/stream")
async def stream(ws: WebSocket):
    await ws.accept()
    out_q: asyncio.Queue = asyncio.Queue()

    async def sender():
        while True:
            msg = await out_q.get()
            await ws.send_text(json.dumps(msg))

    async def send(msg):
        await out_q.put(msg)

    sender_task = asyncio.create_task(sender())
    session: Session | None = None
    finals: set[asyncio.Task] = set()
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                if session:
                    session.add_audio(msg["bytes"])
                continue
            data = json.loads(msg.get("text") or "{}")
            t = data.get("type")
            if t == "start":
                if session and not session.closed:
                    session.cancel()
                session = Session(send, data)
                await send({"type": "started", "id": session.id, "category": session.context["category"]})
            elif t == "mode" and session:
                session.set_mode(data.get("mode", "hold"))
                await send({"type": "mode", "id": session.id, "mode": session.mode})
            elif t == "stop" and session:
                s, session = session, None
                task = asyncio.create_task(s.stop())
                finals.add(task); task.add_done_callback(finals.discard)
            elif t == "cancel" and session:
                session.cancel(); session = None
            elif t == "ping":
                await send({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws")
    finally:
        if session:
            session.cancel()
        for tsk in list(finals):
            tsk.cancel()
        sender_task.cancel()


if __name__ == "__main__":
    import uvicorn
    load_model()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning", ws_max_size=64 * 1024 * 1024)
