"""Keeps Piper voice models loaded, so a paragraph takes ~0.2 s instead of ~6 s.

Listens on 127.0.0.1 only. POST /synth {"text", "voice"} answers WAV bytes;
GET /ping answers "ok". A voice not on disk yet is downloaded on first use.
Runs at lower priority with onnxruntime capped to a few threads, so making
speech never starves audio playback. Exits after IDLE_EXIT seconds unused.
Started on demand by read_aloud.py.
"""
import io
import json
import os
import re
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("CLAUDE_READ_ALOUD_PORT", "59125"))
VOICE_DIR = Path.home() / ".claude" / "read-aloud" / "piper-voices"
IDLE_EXIT = 4 * 3600
ORT_THREADS = 4
LAST_VOICE = VOICE_DIR.parent / "last_voice.txt"  # loaded at start, ready for the next press
# Piper voice names: <lang>_<REGION>-<name>-<quality>, nothing that is a path.
VOICE_NAME = re.compile(r"^[a-z]{2,3}_[A-Z]{2}-[A-Za-z0-9_]+-(x_low|low|medium|high)$")
MAX_TEXT = 5000

_voices = {}
_locks = {}
_load_lock = threading.Lock()
_last_used = time.time()


def _limit_onnx_threads():
    """Piper creates its onnxruntime session with default options (every core);
    hand it options capped at ORT_THREADS instead."""
    import onnxruntime
    original = onnxruntime.SessionOptions
    if getattr(original, "_capped", False):
        return

    def capped():
        o = original()
        o.intra_op_num_threads = ORT_THREADS
        o.inter_op_num_threads = 1
        return o

    capped._capped = True
    onnxruntime.SessionOptions = capped


def get_voice(name):
    if not VOICE_NAME.match(name):
        raise ValueError(f"not a Piper voice name: {name!r}")
    _limit_onnx_threads()
    from piper import PiperVoice
    with _load_lock:
        if name not in _voices:
            model = VOICE_DIR / f"{name}.onnx"
            if not model.exists():
                from piper.download_voices import download_voice
                VOICE_DIR.mkdir(parents=True, exist_ok=True)
                download_voice(name, VOICE_DIR)
            _voices[name] = PiperVoice.load(str(model))
            _locks[name] = threading.Lock()
            try:
                LAST_VOICE.write_text(name, encoding="utf-8")
            except Exception:
                pass
    return _voices[name], _locks[name]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/ping":
            self._send(200, b"ok", "text/plain")
        else:
            self._send(404, b"", "text/plain")

    def do_POST(self):
        global _last_used
        _last_used = time.time()
        if self.path != "/synth":
            return self._send(404, b"", "text/plain")
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > 64 * 1024:
                return self._send(413, b"too large", "text/plain")
            body = json.loads(self.rfile.read(length))
            text = str(body["text"])[:MAX_TEXT]
            speed = min(2.0, max(0.5, float(body.get("speed", 1.0))))
            voice, lock = get_voice(str(body["voice"]))
            from piper import SynthesisConfig
            # Speed is set in the voice itself (phoneme length), so pitch stays.
            config = SynthesisConfig(length_scale=voice.config.length_scale / speed)
            buf = io.BytesIO()
            with lock, wave.open(buf, "wb") as w:  # espeak phonemizer is not thread-safe
                voice.synthesize_wav(text, w, syn_config=config)
            self._send(200, buf.getvalue(), "audio/wav")
        except Exception as e:
            self._send(500, repr(e).encode(), "text/plain")

    def _send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _lower_priority():
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
        else:
            os.nice(5)
    except Exception:
        pass


def main():
    _lower_priority()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def idle_watch():
        while True:
            time.sleep(60)
            if time.time() - _last_used > IDLE_EXIT:
                server.shutdown()
                return

    threading.Thread(target=idle_watch, daemon=True).start()

    def preload():
        try:
            name = LAST_VOICE.read_text(encoding="utf-8").strip()
            if (VOICE_DIR / f"{name}.onnx").exists():
                get_voice(name)
        except Exception:
            pass  # no voice used yet: the first request loads its own

    threading.Thread(target=preload, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
