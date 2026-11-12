"""claude-read-aloud: read Claude's replies aloud, paragraph by paragraph.

Called by the plugin's hooks module (hooks/register.tsx). Speech comes from
Piper (local, offline, fast) with edge-tts (Microsoft online voices) as the
fallback or as an opt-in voice. Audio is played by ffplay (part of FFmpeg).

Modes (all print one line or JSON and exit quickly unless noted):
  (none)              Stop-hook mode: stdin is the hook JSON; prefetches the
                      reply's audio and, if auto-read is on, reads it aloud
  --say-from          stdin JSON {"items":[{key,text}], "start": n}: read the
                      items from n to the end, back to back (returns at once)
  --stop              stop reading
  --speed [n]         set (0.5-2) or print the playback speed
  --voice [name]      set a voice (Piper name, edge-tts *Neural name, "auto")
  --auto [on|off]     auto-read every reply on/off (default off)
  --doctor            JSON report of what is installed and working
  --paths             JSON of the state files the hooks module reads
Internal: --sequence <file>, --prefetch <file>

State lives in ~/.claude/read-aloud/.
"""
import hashlib
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
SPEED_FILE = STATE_DIR / "speed.txt"      # playback speed 0.5-2
PID_FILE = STATE_DIR / "player.pid"
SEQ_PID_FILE = STATE_DIR / "sequence.pid"
NOW_FILE = STATE_DIR / "now.txt"          # key of the paragraph being heard
LOG_FILE = STATE_DIR / "log.txt"
DEBUG_FILE = STATE_DIR / "debug"          # exists -> log playback timing
CACHE_DIR = STATE_DIR / "cache"
INDEX_FILE = STATE_DIR / "cache_index.json"
VOICE_DIR = STATE_DIR / "piper-voices"

KEEP_REPLIES = 2          # audio kept for the latest N replies
MAX_CHARS = 3500          # longest text spoken per paragraph
PREFETCH_THREADS = 8      # parallel edge-tts requests (Piper runs one at a time)
SAMPLE_RATE = 22050       # Piper voices; edge-tts audio is resampled to it
PLAYER_LATENCY = 0.25
SILENCE_BETWEEN = b"\x00\x00" * int(SAMPLE_RATE * 0.35)

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
ABOVE_NORMAL = 0x00008000
BELOW_NORMAL = 0x00004000


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


# ---------- processes (Windows and POSIX) ----------

def popen(args, detached=False, priority=None, **kw):
    if IS_WIN:
        flags = (DETACHED if detached else NO_WINDOW) | (priority or 0)
        return subprocess.Popen(args, creationflags=flags, **kw)
    if detached:
        kw["start_new_session"] = True
    proc = subprocess.Popen(args, **kw)
    if priority == ABOVE_NORMAL:
        pass  # raising priority needs privileges on POSIX
    return proc


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


def spawn(mode, payload):
    """Run this script detached in `mode` with a JSON payload; returns at once."""
    fd, tmp = tempfile.mkstemp(prefix="claude_read_aloud_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    popen([python_for_background(), str(Path(__file__).resolve()), mode, tmp], detached=True,
          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
          close_fds=True)


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
    lang = voice[:2].lower()
    return EDGE_VOICES.get(lang, EDGE_VOICES["en"])


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


# ---------- making audio ----------

def cache_path(text, voice):
    h = hashlib.sha1(f"{voice}\n{text}".encode("utf-8")).hexdigest()[:24]
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


def _ping():
    import urllib.request
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{PIPER_PORT}/ping", timeout=0.5) as r:
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


def piper_synth(text, voice, out_path):
    import urllib.request
    if not ensure_piper_server():
        raise RuntimeError("the Piper server did not start (see --doctor)")
    req = urllib.request.Request(
        f"http://127.0.0.1:{PIPER_PORT}/synth",
        data=json.dumps({"text": text, "voice": voice}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=300) as r:  # first use may download the voice
        Path(out_path).write_bytes(r.read())


def synth_to(text, voice, path):
    """Make one paragraph's audio; written aside, then renamed into place."""
    if path.exists() or wait_for_inflight(path):
        return path
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(f".{os.getpid()}.{time.monotonic_ns()}.part")

    def edge(v):
        import asyncio
        import edge_tts  # slow import (~2 s): only when edge-tts is really used
        asyncio.run(edge_tts.Communicate(text, v).save(str(part)))

    try:
        if is_piper(voice):
            try:
                piper_synth(text, voice, part)
            except Exception as e:
                log(f"piper failed, using edge-tts: {e!r}")
                edge(edge_fallback(voice))
        else:
            edge(voice)
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
    work = []
    for block in reversed(job["blocks"]):  # the final answer first
        texts, voice = block_items([t for t, ok in paragraphs(block) if ok])
        work += [(t, voice, cache_path(t, voice)) for t in texts]
    remember_reply(job["id"], sorted({p.name for _, _, p in work}))
    threads = 1 if all(is_piper(v) for _, v, _ in work) else PREFETCH_THREADS
    with ThreadPoolExecutor(max_workers=threads) as pool:
        for fut in [pool.submit(synth_to, t, v, p) for t, v, p in work]:
            try:
                fut.result()
            except Exception as e:
                log(f"prefetch failed: {e!r}")


# ---------- playback ----------

def find_tool(name):
    return shutil.which(name) or shutil.which(name + ".exe")


def ffplay_cmd(speed):
    ffplay = find_tool("ffplay")
    if not ffplay:
        raise RuntimeError("ffplay not found: install FFmpeg and put it on PATH")
    tempo = ["-af", f"atempo={speed:g}"] if abs(speed - 1.0) > 0.01 else []
    mute = ["-volume", "0"] if os.environ.get("CLAUDE_READ_ALOUD_MUTE") else []  # silent tests
    return [ffplay, "-nodisp", "-autoexit", "-loglevel", "quiet", *mute,
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ch_layout", "mono", *tempo, "-i", "pipe:0"]


def pcm_of(path):
    """A paragraph's audio as raw 16-bit mono PCM at SAMPLE_RATE."""
    import wave
    try:
        with wave.open(str(path), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (SAMPLE_RATE, 1, 2):
                return w.readframes(w.getnframes())
    except (wave.Error, EOFError):
        pass
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found: install FFmpeg and put it on PATH")
    kw = {"creationflags": NO_WINDOW} if IS_WIN else {}
    out = subprocess.run([ffmpeg, "-loglevel", "quiet", "-i", str(path), "-f", "s16le",
                          "-ar", str(SAMPLE_RATE), "-ac", "1", "-"],
                         capture_output=True, check=True, **kw)
    return out.stdout


def stop_playing():
    kill_from(SEQ_PID_FILE)
    unlink(NOW_FILE)
    kill_from(PID_FILE)


def sequence_worker(job_file):
    """Read paragraphs back to back through ONE player (no gaps); the next
    paragraph is made while the current one plays."""
    from concurrent.futures import ThreadPoolExecutor
    job = json.loads(Path(job_file).read_text(encoding="utf-8"))
    os.remove(job_file)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SEQ_PID_FILE.write_text(str(os.getpid()))
    raw = job["items"]
    start = job.get("start", 0)
    _, voice = block_items([it["text"] for it in raw])
    items = [(it["key"], clean(it["text"])) for it in raw[start:]]
    items = [(k, t) for k, t in items if t]
    if not items:
        unlink(SEQ_PID_FILE)
        return
    pool = ThreadPoolExecutor(max_workers=1)
    futures = [pool.submit(synth_to, t, voice, cache_path(t, voice)) for _, t in items]

    speed = get_speed()
    bytes_per_sec = SAMPLE_RATE * 2 * speed
    try:
        player = popen(ffplay_cmd(speed), priority=ABOVE_NORMAL, stdin=subprocess.PIPE)
    except Exception as e:
        log(f"cannot start player: {e!r}")
        unlink(SEQ_PID_FILE)
        return
    PID_FILE.write_text(str(player.pid))

    # What is heard lags what is written, so NOW_FILE follows a schedule.
    schedule = []
    done = threading.Event()

    def follow():
        shown = None
        while not done.is_set():
            now = time.time()
            current = None
            for at, key in list(schedule):
                if at <= now:
                    current = key
            if current and current != shown:
                NOW_FILE.write_text(current, encoding="utf-8")
                shown = current
            time.sleep(0.05)

    t_begin = time.time()
    end_at = [t_begin + PLAYER_LATENCY]
    follower = threading.Thread(target=follow, daemon=True)
    follower.start()
    try:
        for (key, _), fut in zip(items, futures):
            try:
                pcm = pcm_of(fut.result())
            except Exception as e:
                log(f"paragraph failed: {e!r}")
                continue
            starts = max(end_at[0], time.time() + PLAYER_LATENCY)
            end_at[0] = starts + len(pcm) / bytes_per_sec
            schedule.append((starts, key))
            if DEBUG_FILE.exists():
                log(f"sched {key} at +{starts - t_begin:.2f}s, "
                    f"{len(pcm) / bytes_per_sec:.2f}s long @ {speed:g}x")
            player.stdin.write(pcm + SILENCE_BETWEEN)
            end_at[0] += len(SILENCE_BETWEEN) / bytes_per_sec
            player.stdin.flush()
        # ffplay quits at end of input, not end of sound: feed silence until
        # everything written has been heard, then close.
        while time.time() < end_at[0] + 0.6 and player.poll() is None:
            player.stdin.write(b"\x00\x00" * (SAMPLE_RATE // 10))
            player.stdin.flush()
            time.sleep(0.1)
        player.stdin.close()
        try:
            player.wait(timeout=1)
        except subprocess.TimeoutExpired:
            player.kill()
    except (BrokenPipeError, OSError):
        pass  # stopped
    finally:
        done.set()
        follower.join(timeout=1)
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
    for mod in ("piper", "edge_tts"):
        try:
            __import__(mod)
            report[mod] = True
        except Exception as e:
            report[mod] = f"missing ({e.__class__.__name__})"
    report["ffplay"] = find_tool("ffplay") or "missing"
    report["ffmpeg"] = find_tool("ffmpeg") or "missing"
    report["piper_server"] = "running" if _ping() else "not running (starts on first use)"
    report["voices_downloaded"] = sorted(p.stem for p in VOICE_DIR.glob("*.onnx"))
    report["voice"] = VOICE_FILE.read_text().strip() if VOICE_FILE.exists() else "auto"
    report["speed"] = get_speed()
    report["auto_read"] = AUTO_FILE.exists()
    report["ok"] = report["piper"] is True and report["ffplay"] != "missing"
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
    elif cmd == "--doctor":
        print(json.dumps(doctor()))
    elif cmd == "--paths":
        print(json.dumps({"now": str(NOW_FILE), "speed": str(SPEED_FILE)}))


if __name__ == "__main__":
    main()
