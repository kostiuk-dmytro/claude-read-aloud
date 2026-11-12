"""claude-read-aloud: read Claude's replies aloud, paragraph by paragraph.

Called by the plugin's hooks module (hooks/register.tsx). Speech comes from
Piper (local, offline, fast) with edge-tts (Microsoft online voices) as the
fallback or as an opt-in voice. Audio is played with miniaudio (no FFmpeg
needed); ffplay is used only if miniaudio is missing.

This file runs with only the standard library until speech is needed, so
any Python 3.9+ can run --paths, --setup and --doctor. --setup makes a
private virtual environment with the speech packages; the hooks module then
runs everything else with that environment's Python.

Modes (each prints one line or JSON and returns quickly unless noted):
  (none)              Stop-hook mode: stdin is the hook JSON; prefetches the
                      reply's audio and, if auto-read is on, reads it aloud
  --say-from          stdin JSON {"items":[{key,text}], "start": n}: read the
                      items from n to the end, back to back (returns at once)
  --stop              stop reading
  --speed [n]         set (0.5-2) or print the speaking speed
  --voice [name]      set a voice (Piper name, edge-tts *Neural name, "auto")
  --auto [on|off]     auto-read every reply on/off (default off)
  --setup             install the speech packages into a private venv
                      (runs in the background; --paths reports progress)
  --doctor            JSON report of what is installed and working
  --paths             JSON: state files, the Python to use, readiness
Internal: --sequence <file>, --prefetch <file>, --setup-worker

State lives in ~/.claude/read-aloud/.
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

IS_WIN = os.name == "nt"
HERE = Path(__file__).resolve().parent

STATE_DIR = Path.home() / ".claude" / "read-aloud"
AUTO_FILE = STATE_DIR / "auto"            # exists -> every reply is read aloud
VOICE_FILE = STATE_DIR / "voice.txt"      # fixed voice; absent -> by language
SPEED_FILE = STATE_DIR / "speed.txt"      # speaking speed 0.5-2
PID_FILE = STATE_DIR / "player.pid"
SEQ_PID_FILE = STATE_DIR / "sequence.pid"
NOW_FILE = STATE_DIR / "now.txt"          # key of the paragraph being heard
LOG_FILE = STATE_DIR / "log.txt"
DEBUG_FILE = STATE_DIR / "debug"          # exists -> log playback timing
CACHE_DIR = STATE_DIR / "cache"
INDEX_FILE = STATE_DIR / "cache_index.json"
VOICE_DIR = STATE_DIR / "piper-voices"
VENV_DIR = STATE_DIR / "venv"
VENV_READY = VENV_DIR / ".ready"          # written once the packages installed
SETUP_STATUS = STATE_DIR / "setup.json"   # {"state": running|done|failed, ...}
SETUP_LOG = STATE_DIR / "setup-log.txt"

PACKAGES = ["piper-tts>=1.3", "edge-tts>=7.0", "miniaudio>=1.59"]
KEEP_REPLIES = 2          # audio kept for the latest N replies
MAX_CHARS = 3500          # longest text spoken per paragraph
PREFETCH_THREADS = 8      # parallel edge-tts requests (Piper runs one at a time)
SAMPLE_RATE = 22050       # Piper voices; edge-tts audio is resampled to it
BYTES_PER_SEC = SAMPLE_RATE * 2
SILENCE_BETWEEN = b"\x00\x00" * int(SAMPLE_RATE * 0.35)
MUTED = bool(os.environ.get("CLAUDE_READ_ALOUD_MUTE"))  # silent playback for tests

PIPER_PORT = int(os.environ.get("CLAUDE_READ_ALOUD_PORT", "59125"))
PIPER_SERVER = HERE / "piper_server.py"

# Voice picked by the reply's language when no fixed voice is set.
PIPER_VOICES = {
    "en": "en_US-lessac-medium",
    "sv": "sv_SE-lisa-medium",
    "uk": "uk_UA-tetiana-high",
}
EDGE_VOICES = {
    "en": "en-US-AvaNeural",
    "sv": "sv-SE-SofieNeural",
    "uk": "uk-UA-PolinaNeural",
}

NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200 | NO_WINDOW


def log(msg):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except Exception:
        pass


def unlink(*paths):
    for p in paths:
        try:
            p.unlink()
        except Exception:
            pass


def has_module(name):
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


# ---------- processes (Windows and POSIX) ----------

def popen(args, detached=False, **kw):
    if IS_WIN:
        return subprocess.Popen(args, creationflags=DETACHED if detached else NO_WINDOW, **kw)
    if detached:
        kw["start_new_session"] = True
    return subprocess.Popen(args, **kw)


def python_for_background():
    """pythonw on Windows (no console window), the same interpreter elsewhere."""
    if IS_WIN:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return sys.executable


def kill_tree(pid):
    if pid == os.getpid():
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, creationflags=NO_WINDOW)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        pass


def kill_from(pid_file):
    try:
        kill_tree(int(pid_file.read_text().strip()))
    except Exception:
        pass
    unlink(pid_file)


def spawn(mode, payload=None):
    """Run this script detached in `mode` (with a JSON payload); returns at once."""
    args = [python_for_background(), str(Path(__file__).resolve()), mode]
    if payload is not None:
        fd, tmp = tempfile.mkstemp(prefix="claude_read_aloud_", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        args.append(tmp)
    popen(args, detached=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL, close_fds=True)


# ---------- setup: a private venv with the speech packages ----------

def venv_python():
    if IS_WIN:
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python3"


def read_setup_status():
    try:
        return json.loads(SETUP_STATUS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def setup_running():
    st = read_setup_status()
    return st.get("state") == "running" and time.time() - st.get("since", 0) < 900


def start_setup():
    if VENV_READY.exists() and venv_python().exists():
        return "ready"
    if setup_running():
        return "running"
    SETUP_STATUS.write_text(json.dumps({"state": "running", "since": time.time()}), encoding="utf-8")
    spawn("--setup-worker")
    return "started"


def setup_worker():
    """Make the venv and pip-install the packages; record how it went."""
    def fail(msg):
        SETUP_STATUS.write_text(json.dumps({"state": "failed", "error": msg}), encoding="utf-8")
        log(f"setup failed: {msg}")

    kw = {"creationflags": NO_WINDOW} if IS_WIN else {}
    with open(SETUP_LOG, "w", encoding="utf-8") as out:
        try:
            if not venv_python().exists():
                r = subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)],
                                   stdout=out, stderr=subprocess.STDOUT, **kw)
                if r.returncode != 0 or not venv_python().exists():
                    return fail("could not create a virtual environment "
                                "(on Debian/Ubuntu: sudo apt install python3-venv)")
            r = subprocess.run([str(venv_python()), "-m", "pip", "install",
                                "--disable-pip-version-check", *PACKAGES],
                               stdout=out, stderr=subprocess.STDOUT, **kw)
            if r.returncode != 0:
                return fail(f"pip install failed, see {SETUP_LOG}")
        except Exception as e:
            return fail(repr(e))
    VENV_READY.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
    SETUP_STATUS.write_text(json.dumps({"state": "done"}), encoding="utf-8")


def paths_report():
    """What the hooks module needs: which Python to run, and whether it can."""
    report = {"now": str(NOW_FILE), "speed": str(SPEED_FILE)}
    if VENV_READY.exists() and venv_python().exists():
        report.update(ready=True, python=str(venv_python()), problem=None)
    elif has_module("piper") and (has_module("miniaudio") or find_tool("ffplay")):
        report.update(ready=True, python=sys.executable, problem=None)  # installed by hand
    else:
        st = read_setup_status()
        if setup_running():
            problem = "Setting up read-aloud (one time, 1-2 minutes)..."
        elif st.get("state") == "failed":
            problem = f"Setup failed: {st.get('error')}. Try /read-aloud setup again."
        else:
            problem = "Read-aloud needs a one-time setup. Press 🔊 or run /read-aloud setup."
        report.update(ready=False, python=None, problem=problem,
                      setup=st.get("state", "not started"))
    return report


# ---------- settings ----------

def get_speed():
    try:
        return min(2.0, max(0.5, float(SPEED_FILE.read_text().strip())))
    except Exception:
        return 1.0


def is_piper(voice):
    return not voice.endswith("Neural")


def detect_language(text):
    if re.search(r"[Ѐ-ӿ]", text):
        return "uk"
    words = re.findall(r"[a-zåäö]+", text.lower())
    sv = sum(w in {"och", "det", "är", "jag", "att", "som", "inte", "på", "för",
                   "med", "du", "en", "har", "kan"} for w in words)
    sv += len(re.findall(r"[åäö]", text.lower()))
    en = sum(w in {"the", "and", "is", "to", "of", "you", "it", "that", "for",
                   "with", "this", "are"} for w in words)
    return "sv" if sv > en else "en"


def pick_voice(text):
    try:
        fixed = VOICE_FILE.read_text(encoding="utf-8").strip()
        if fixed:
            return fixed
    except Exception:
        pass
    return PIPER_VOICES[detect_language(text)]


def edge_fallback(voice):
    return EDGE_VOICES.get(voice[:2].lower(), EDGE_VOICES["en"])


# ---------- reading the transcript ----------

def reply_blocks_from_transcript(path):
    """All text blocks Claude wrote since the person's last prompt, in order."""
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except Exception:
                continue
    blocks = []
    for e in reversed(entries):
        kind = e.get("type")
        content = (e.get("message") or {}).get("content")
        if kind == "user":
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
            if not is_tool_result:
                break
            continue
        if kind != "assistant":
            continue
        if isinstance(content, str):
            blocks.append(content)
        elif isinstance(content, list):
            for b in reversed(content):
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    blocks.append(b["text"])
    return list(reversed(blocks))


# ---------- paragraphs (must match hooks/register.tsx) ----------

FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6}\s.*|\*\*[^*\n]+\*\*:?)$")


def paragraphs(md):
    chunks, cur, in_fence = [], [], False
    for line in md.split("\n"):
        if FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and line.strip() == "":
            if cur:
                chunks.append("\n".join(cur))
            cur = []
        else:
            cur.append(line)
    if cur:
        chunks.append("\n".join(cur))
    out, i = [], 0
    while i < len(chunks):
        text = chunks[i]
        after = chunks[i + 1] if i + 1 < len(chunks) else None
        if HEADING.match(text.strip()) and after is not None and not FENCE.match(after):
            text = f"{text}\n\n{after}"
            i += 1
        is_code = bool(FENCE.match(text)) and bool(re.search(r"(```|~~~)\s*$", text))
        is_table = text.strip().startswith("|")
        out.append((text, not is_code and not is_table))
        i += 1
    return out


def clean(text):
    """Markdown to speakable text: drop code, tables, links' targets, emoji."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("|") or re.fullmatch(r"[-*_=]{3,}", s):
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^>\s*", "", s)
        s = re.sub(r"^([-*+]|\d+[.)])\s+", "", s)
        if s and not re.search(r"[.!?:;,]$", s):
            s += "."
        lines.append(s)
    text = "\n".join(lines)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_~]{1,3}", "", text)
    text = re.sub(r"[←-⇿☀-➿\U0001F000-\U0001FAFF]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_CHARS:
        cut = text[:MAX_CHARS]
        text = cut[: cut.rfind(".") + 1 or MAX_CHARS]
    return text


def block_items(block_texts):
    """Cleaned speakable paragraphs of one block, plus the block's voice."""
    texts = [t for t in (clean(t) for t in block_texts) if t]
    return texts, pick_voice(" ".join(texts))


# ---------- making audio (speed is part of the audio, so it keeps its pitch) ----------

def cache_path(text, voice, speed):
    h = hashlib.sha1(f"{voice}\n{speed:g}\n{text}".encode("utf-8")).hexdigest()[:24]
    return CACHE_DIR / f"{h}.{'wav' if is_piper(voice) else 'mp3'}"


def wait_for_inflight(path, timeout=25):
    """If another process is already making this file, wait for it."""
    end = time.time() + timeout
    while not path.exists() and time.time() < end:
        fresh = [p for p in CACHE_DIR.glob(path.stem + ".*.part")
                 if time.time() - p.stat().st_mtime < 15]
        if not fresh:
            break
        time.sleep(0.05)
    return path.exists()


def _opener():
    import urllib.request
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ping():
    try:
        with _opener().open(f"http://127.0.0.1:{PIPER_PORT}/ping", timeout=0.5) as r:
            return r.read() == b"ok"
    except Exception:
        return False


def ensure_piper_server(timeout=60):
    """Start the resident Piper server if it is not running; True once it answers."""
    if _ping():
        return True
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock = STATE_DIR / "piper_server.starting"
    try:
        fresh = lock.exists() and time.time() - lock.stat().st_mtime < timeout
    except Exception:
        fresh = False
    if not fresh:
        lock.touch()
        popen([python_for_background(), str(PIPER_SERVER)], detached=True,
              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
    end = time.time() + timeout
    while time.time() < end:
        if _ping():
            unlink(lock)
            return True
        time.sleep(0.1)
    return False


def piper_synth(text, voice, speed, out_path):
    import urllib.request
    if not ensure_piper_server():
        raise RuntimeError("the Piper server did not start (see /read-aloud doctor)")
    req = urllib.request.Request(
        f"http://127.0.0.1:{PIPER_PORT}/synth",
        data=json.dumps({"text": text, "voice": voice, "speed": speed}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with _opener().open(req, timeout=300) as r:  # first use may download the voice
        Path(out_path).write_bytes(r.read())


def edge_synth(text, voice, speed, out_path):
    import asyncio
    import edge_tts  # slow import (~2 s): only when edge-tts is really used
    rate = f"{round((speed - 1) * 100):+d}%"
    asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(out_path)))


def synth_to(text, voice, speed, path):
    """Make one paragraph's audio; written aside, then renamed into place."""
    if path.exists() or wait_for_inflight(path):
        return path
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(f".{os.getpid()}.{time.monotonic_ns()}.part")
    try:
        if is_piper(voice):
            try:
                piper_synth(text, voice, speed, part)
            except Exception as e:
                log(f"piper failed, using edge-tts: {e!r}")
                edge_synth(text, edge_fallback(voice), speed, part)
        else:
            edge_synth(text, voice, speed, part)
        try:
            part.replace(path)
        except PermissionError:
            if not path.exists():
                raise
    finally:
        unlink(part)
    return path


def remember_reply(reply_id, files):
    """Record this reply's files; delete audio of replies older than KEEP_REPLIES."""
    try:
        index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        index = []
    index = [r for r in index if r.get("id") != reply_id]
    index.insert(0, {"id": reply_id, "files": files})
    keep, drop = index[:KEEP_REPLIES], index[KEEP_REPLIES:]
    kept = {f for r in keep for f in r["files"]}
    for r in drop:
        for f in r["files"]:
            if f not in kept:
                unlink(CACHE_DIR / f)
    INDEX_FILE.write_text(json.dumps(keep), encoding="utf-8")
    for p in CACHE_DIR.glob("*"):
        try:
            stale = p.suffix in (".mp3", ".wav") or time.time() - p.stat().st_mtime > 600
            if p.name not in kept and stale:
                p.unlink()
        except Exception:
            pass


def prefetch_worker(job_file):
    from concurrent.futures import ThreadPoolExecutor
    job = json.loads(Path(job_file).read_text(encoding="utf-8"))
    os.remove(job_file)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    speed = get_speed()
    work = []
    for block in reversed(job["blocks"]):  # the final answer first
        texts, voice = block_items([t for t, ok in paragraphs(block) if ok])
        work += [(t, voice, cache_path(t, voice, speed)) for t in texts]
    remember_reply(job["id"], sorted({p.name for _, _, p in work}))
    threads = 1 if all(is_piper(v) for _, v, _ in work) else PREFETCH_THREADS
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for fut in [pool.submit(synth_to, t, v, speed, p) for t, v, p in work]:
            try:
                fut.result()
            except Exception as e:
                log(f"prefetch failed: {e!r}")


# ---------- playback ----------

def find_tool(name):
    found = shutil.which(name) or shutil.which(name + ".exe")
    if found:
        return found
    for folder in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"):  # macOS GUI apps
        candidate = Path(folder) / name
        if candidate.exists():
            return str(candidate)
    return None


def pcm_of(path):
    """A paragraph's audio as raw 16-bit mono PCM at SAMPLE_RATE."""
    import wave
    try:
        with wave.open(str(path), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SAMPLE_RATE, 1, 2):
                return w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        pass
    if has_module("miniaudio"):  # mp3 from edge-tts
        import miniaudio
        decoded = miniaudio.decode_file(str(path), output_format=miniaudio.SampleFormat.SIGNED16,
                                        nchannels=1, sample_rate=SAMPLE_RATE)
        return decoded.samples.tobytes()
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("cannot decode mp3: run /read-aloud setup")
    kw = {"creationflags": NO_WINDOW} if IS_WIN else {}
    out = subprocess.run([ffmpeg, "-loglevel", "quiet", "-i", str(path), "-f", "s16le",
                          "-ar", str(SAMPLE_RATE), "-ac", "1", "-"],
                         capture_output=True, check=True, **kw)
    return out.stdout


class MiniaudioPlayer:
    """One open output stream fed paragraph after paragraph: no gaps, and it
    knows exactly how much has been heard."""

    def __init__(self):
        import miniaudio
        self._buf = bytearray()
        self._lock = threading.Lock()
        self.written = 0
        self.played = 0
        self._device = miniaudio.PlaybackDevice(
            output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1,
            sample_rate=SAMPLE_RATE, buffersize_msec=100)
        gen = self._feed()
        next(gen)
        self._device.start(gen)

    def _feed(self):
        frames = yield b""
        while True:
            want = frames * 2
            with self._lock:
                chunk = bytes(self._buf[:want])
                del self._buf[:want]
                self.played += len(chunk)
            if MUTED:
                chunk = b"\x00" * len(chunk)
            frames = yield chunk + b"\x00" * (want - len(chunk))

    def write(self, pcm):
        with self._lock:
            self._buf += pcm
            self.written += len(pcm)

    def position(self):
        return self.played

    def drain(self):
        while self.played < self.written:
            time.sleep(0.05)
        time.sleep(0.25)  # the device's own buffer

    def close(self):
        try:
            self._device.close()
        except Exception:
            pass


class FfplayPlayer:
    """Fallback when miniaudio is missing: raw PCM piped into ffplay."""

    def __init__(self):
        ffplay = find_tool("ffplay")
        if not ffplay:
            raise RuntimeError("no audio output: run /read-aloud setup")
        mute = ["-volume", "0"] if MUTED else []
        self._proc = popen([ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", *mute,
                            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ch_layout", "mono",
                            "-i", "pipe:0"], stdin=subprocess.PIPE)
        PID_FILE.write_text(str(self._proc.pid))
        self.written = 0
        self._started = None

    def write(self, pcm):
        if self._started is None:
            self._started = time.time() + 0.25
        # After an underrun the stream restarts from now.
        heard = self.position()
        if heard >= self.written:
            self._started = time.time() + 0.25 - self.written / BYTES_PER_SEC
        self._proc.stdin.write(pcm)
        self._proc.stdin.flush()
        self.written += len(pcm)

    def position(self):
        if self._started is None:
            return 0
        return max(0, min(self.written, int((time.time() - self._started) * BYTES_PER_SEC)))

    def drain(self):
        # ffplay quits at end of input, not end of sound: keep it fed with silence.
        while self.position() < self.written and self._proc.poll() is None:
            self._proc.stdin.write(b"\x00\x00" * (SAMPLE_RATE // 10))
            self._proc.stdin.flush()
            time.sleep(0.1)
        time.sleep(0.4)

    def close(self):
        try:
            self._proc.stdin.close()
            self._proc.wait(timeout=1)
        except Exception:
            self._proc.kill()


def stop_playing():
    kill_from(SEQ_PID_FILE)
    unlink(NOW_FILE)
    kill_from(PID_FILE)


def sequence_worker(job_file):
    """Read paragraphs back to back through ONE output stream; the next
    paragraph is made while the current one plays."""
    from concurrent.futures import ThreadPoolExecutor
    job = json.loads(Path(job_file).read_text(encoding="utf-8"))
    os.remove(job_file)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SEQ_PID_FILE.write_text(str(os.getpid()))
    raw = job["items"]
    start = job.get("start", 0)
    speed = get_speed()
    _, voice = block_items([it["text"] for it in raw])
    items = [(it["key"], clean(it["text"])) for it in raw[start:]]
    items = [(k, t) for k, t in items if t]
    if not items:
        unlink(SEQ_PID_FILE)
        return
    pool = ThreadPoolExecutor(max_workers=1)
    futures = [pool.submit(synth_to, t, voice, speed, cache_path(t, voice, speed))
               for _, t in items]
    try:
        player = MiniaudioPlayer() if has_module("miniaudio") else FfplayPlayer()
    except Exception as e:
        log(f"cannot open audio output: {e!r}")
        unlink(SEQ_PID_FILE)
        return

    # NOW_FILE follows what is heard: each paragraph starts at a byte offset.
    schedule = []
    done = threading.Event()

    def follow():
        shown = None
        while not done.is_set():
            pos = player.position()
            current = None
            for offset, key in list(schedule):
                if offset <= pos:
                    current = key
            if current and current != shown:
                # Written aside and renamed, so a reader never sees it half-written.
                tmp = NOW_FILE.with_suffix(".tmp")
                tmp.write_text(current, encoding="utf-8")
                try:
                    tmp.replace(NOW_FILE)
                except OSError:
                    NOW_FILE.write_text(current, encoding="utf-8")
                shown = current
            time.sleep(0.05)

    follower = threading.Thread(target=follow, daemon=True)
    follower.start()
    try:
        for (key, _), fut in zip(items, futures):
            try:
                pcm = pcm_of(fut.result())
            except Exception as e:
                log(f"paragraph failed: {e!r}")
                continue
            schedule.append((player.written, key))
            if DEBUG_FILE.exists():
                log(f"queued {key} at byte {player.written}, {len(pcm) / BYTES_PER_SEC:.2f}s")
            player.write(pcm + SILENCE_BETWEEN)
        player.drain()
    except (BrokenPipeError, OSError):
        pass  # stopped
    finally:
        done.set()
        follower.join(timeout=1)
        player.close()
        pool.shutdown(wait=False, cancel_futures=True)
        unlink(NOW_FILE, PID_FILE, SEQ_PID_FILE)


# ---------- entry points ----------

def hook():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace").strip() or "{}")
    blocks = []
    if data.get("transcript_path"):
        try:
            blocks = reply_blocks_from_transcript(data["transcript_path"])
        except Exception as e:
            log(f"transcript read failed: {e!r}")
    last = data.get("last_assistant_message")
    if last and (not blocks or blocks[-1].strip() != last.strip()):
        blocks.append(last)
    if not blocks:
        return
    reply_id = f"{data.get('session_id', '')}-{time.time_ns()}"
    spawn("--prefetch", {"id": reply_id, "blocks": blocks})
    if AUTO_FILE.exists():
        paras = [t for t, ok in paragraphs(blocks[-1]) if ok]
        stop_playing()
        spawn("--sequence", {"items": [{"key": f"auto-{i}", "text": t}
                                       for i, t in enumerate(paras)], "start": 0})


def doctor():
    report = {"python": sys.executable, "platform": sys.platform}
    report.update({k: v for k, v in paths_report().items() if k in ("ready", "problem", "setup")})
    report["private_venv"] = str(VENV_DIR) if VENV_READY.exists() else "not set up"
    for mod in ("piper", "edge_tts", "miniaudio"):
        report[mod] = has_module(mod)
    report["ffplay_fallback"] = find_tool("ffplay") or "not found (not needed)"
    report["piper_server"] = "running" if _ping() else "not running (starts on first use)"
    report["voices_downloaded"] = sorted(p.stem for p in VOICE_DIR.glob("*.onnx"))
    report["voice"] = VOICE_FILE.read_text().strip() if VOICE_FILE.exists() else "auto"
    report["speed"] = get_speed()
    report["auto_read"] = AUTO_FILE.exists()
    return report


def main():
    args = sys.argv[1:]
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not args:
        try:
            hook()
        except Exception as e:
            log(f"hook error: {e!r}")
        return
    cmd = args[0]
    if cmd == "--say-from":
        job = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace"))
        stop_playing()
        spawn("--sequence", job)
    elif cmd == "--sequence":
        sequence_worker(args[1])
    elif cmd == "--prefetch":
        prefetch_worker(args[1])
    elif cmd == "--stop":
        stop_playing()
        print("Stopped.")
    elif cmd == "--speed":
        if len(args) > 1:
            try:
                value = min(2.0, max(0.5, float(args[1].replace(",", ".").rstrip("x×"))))
            except ValueError:
                print("Not a speed: use a number from 0.5 to 2, e.g. 1.25")
                return
            SPEED_FILE.write_text(f"{value:g}")
        print(f"{get_speed():g}")
    elif cmd == "--voice":
        name = args[1].strip() if len(args) > 1 else ""
        if name in ("", "auto"):
            unlink(VOICE_FILE)
            print("Voice: automatic by language.")
        elif not re.fullmatch(r"[A-Za-z]{2,3}[-_][A-Za-z]{2}[-_][A-Za-z0-9_]+(-[A-Za-z_]+)?", name):
            print(f"Not a voice name: {name}")
        else:
            VOICE_FILE.write_text(name, encoding="utf-8")
            print(f"Voice: {name}")
    elif cmd == "--auto":
        if len(args) > 1 and args[1].lower() in ("on", "off"):
            if args[1].lower() == "on":
                AUTO_FILE.touch()
            else:
                unlink(AUTO_FILE)
        print("on" if AUTO_FILE.exists() else "off")
    elif cmd == "--setup":
        state = start_setup()
        print({"ready": "Read-aloud is already set up.",
               "running": "Setup is already running (1-2 minutes).",
               "started": "Setting up read-aloud (one time, 1-2 minutes)..."}[state])
    elif cmd == "--setup-worker":
        setup_worker()
    elif cmd == "--warm":
        # Session start: get the voice server loading now, so the first press
        # does not wait for it. Returns at once.
        if paths_report()["ready"] and not _ping():
            spawn("--warm-worker")
    elif cmd == "--warm-worker":
        ensure_piper_server()
    elif cmd == "--doctor":
        print(json.dumps(doctor()))
    elif cmd == "--paths":
        print(json.dumps(paths_report()))


if __name__ == "__main__":
    main()
