"""Stream a PCM file over the /stream websocket like the client would; print partials and finals."""
import json, sys, time, threading, wave
import websocket

port = int(sys.argv[1]); pcm_path = sys.argv[2]; mode = sys.argv[3] if len(sys.argv) > 3 else "hold"
speed = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
pcm = open(pcm_path, "rb").read()
ws = websocket.create_connection(f"ws://127.0.0.1:{port}/stream")
t0 = time.time()
done = threading.Event()
def reader():
    while True:
        try:
            m = json.loads(ws.recv())
        except Exception:
            break
        t = time.time() - t0
        if m["type"] == "partial":
            print(f"{t:6.2f}s partial ({m['audio_s']}s, asr {m['asr_ms']}ms): {m['text'][-90:]!r}")
        elif m["type"] == "final":
            print(f"{t:6.2f}s FINAL seg{m.get('segment')} {m.get('timings')} llm={m.get('used_llm')} cat={m.get('category')}\n        raw : {m.get('raw')!r}\n        text: {m['text']!r}")
            if mode == "hold" or m["text"] == "":
                done.set()
        else:
            print(f"{t:6.2f}s {m}")
threading.Thread(target=reader, daemon=True).start()
ws.send(json.dumps({"type": "start", "mode": "hold", "context": {"bundle": "com.apple.Notes", "app": "Notes", "title": "Test"}}))
if mode == "auto":
    ws.send(json.dumps({"type": "mode", "mode": "auto"}))
CH = 2048  # bytes = 1024 samples = 64 ms
for i in range(0, len(pcm), CH):
    ws.send_binary(pcm[i:i + CH]); time.sleep(0.064 / speed)
t_stop = time.time()
if mode == "auto":
    print(f"{t_stop-t0:6.2f}s clip ended; streaming silence")
    for i in range(int(2.5 / 0.064)):
        ws.send_binary(bytes(2048)); time.sleep(0.064)
    t_stop = time.time()
ws.send(json.dumps({"type": "stop"}))
print(f"{t_stop-t0:6.2f}s stop sent")
done.wait(10)
print(f"stop->final latency: {(time.time()-t_stop)*1000:.0f} ms")
ws.close()
