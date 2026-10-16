from datetime import datetime, timezone, timedelta
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait
import io
import json
import base64
import ctypes
import os
import re
import threading
import wave
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote_plus, urlencode, urlparse
import webbrowser


APP_DIR = Path(__file__).resolve().parent
PAGE = APP_DIR / "ollama-chat.html"
MEMORY_DIR = APP_DIR / "Arc Memory"
CHATS_FILE = MEMORY_DIR / "conversations.json"
MEMORIES_FILE = MEMORY_DIR / "memories.json"
STATE_FILE = MEMORY_DIR / "review-state.json"
OPENAI_KEY_FILE = MEMORY_DIR / "openai-key.dat"
CLOUDFLARE_ACCOUNT_FILE = MEMORY_DIR / "cloudflare-account.dat"
CLOUDFLARE_TOKEN_FILE = MEMORY_DIR / "cloudflare-token.dat"
PIPER_DIR = APP_DIR / "Arc Piper"
PIPER_PACKAGES = PIPER_DIR / "python-packages"
PIPER_MODEL = PIPER_DIR / "voices" / "en_US-amy-medium.onnx"
ADDRESS = ("127.0.0.1", 8766)
APP_VERSION = "4.2.0"
MEMORY_DIR.mkdir(parents=True, exist_ok=True)
SEARCH_POOL = ThreadPoolExecutor(max_workers=8)
PIPER_LOCK = threading.Lock()
PIPER_VOICE = None
PIPER_ERROR = ""


def get_piper_voice():
    """Load Piper once; keep it warm so later speech chunks start quickly."""
    global PIPER_VOICE, PIPER_ERROR
    if PIPER_VOICE is not None:
        return PIPER_VOICE
    with PIPER_LOCK:
        if PIPER_VOICE is not None:
            return PIPER_VOICE
        if not PIPER_MODEL.is_file() or not PIPER_MODEL.with_suffix(PIPER_MODEL.suffix + ".json").is_file():
            raise RuntimeError("Piper's Amy voice files are missing from Arc Piper\\voices.")
        if str(PIPER_PACKAGES) not in os.sys.path:
            os.sys.path.insert(0, str(PIPER_PACKAGES))
        try:
            from piper import PiperVoice
            PIPER_VOICE = PiperVoice.load(str(PIPER_MODEL))
            PIPER_ERROR = ""
            return PIPER_VOICE
        except Exception as exc:
            PIPER_ERROR = str(exc)
            raise RuntimeError("Piper could not load the Amy voice: " + PIPER_ERROR) from exc


def synthesize_piper(text):
    voice = get_piper_voice()
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    return audio.getvalue()


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data):
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def dpapi_protect(data):
    if os.name != "nt":
        raise OSError("Windows DPAPI is only available on Windows.")
    source, keepalive = _blob(data)
    output = DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_wchar_p,
                                         ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptProtectData.restype = ctypes.c_int
    if not crypt32.CryptProtectData(ctypes.byref(source), "Arc OpenAI API key", None, None, None, 0x1, ctypes.byref(output)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def dpapi_unprotect(data):
    if os.name != "nt":
        return ""
    source, keepalive = _blob(data)
    output = DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.POINTER(ctypes.c_wchar_p),
                                           ctypes.POINTER(DATA_BLOB), ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(output)):
        return ""
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def load_openai_key():
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return dpapi_unprotect(OPENAI_KEY_FILE.read_bytes())
    except OSError:
        return ""

    
def load_cloudflare_account_id():
    env_value = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if env_value:
        return env_value
    try:
        return dpapi_unprotect(CLOUDFLARE_ACCOUNT_FILE.read_bytes())
    except OSError:
        return ""


def load_cloudflare_token():
    env_value = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if env_value:
        return env_value
    try:
        return dpapi_unprotect(CLOUDFLARE_TOKEN_FILE.read_bytes())
    except OSError:
        return ""


def save_cloudflare_credentials(account_id, token):
    account_id = account_id.strip()
    token = token.strip()
    if not account_id or not token:
        raise ValueError("Enter both your Cloudflare Account ID and API token.")
    account_file = dpapi_protect(account_id.encode("utf-8"))
    token_file = dpapi_protect(token.encode("utf-8"))
    CLOUDFLARE_ACCOUNT_FILE.write_bytes(account_file)
    CLOUDFLARE_TOKEN_FILE.write_bytes(token_file)


def openai_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace"))
            message = detail.get("error", {}).get("message", "")
        except Exception:
            message = ""
        return message or "OpenAI API returned HTTP " + str(exc.code) + ". Check the API key and API billing."
    return "Could not reach OpenAI: " + str(exc)


class SearchResultsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.capture = None
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if self.capture:
            if tag not in ("br", "img", "meta", "input"):
                self.depth += 1
            return
        if tag == "a" and "result__a" in classes:
            self.capture = {"type": "title", "url": attrs.get("href", ""), "text": ""}
            self.depth = 1
        elif tag == "a" and "result__snippet" in classes:
            self.capture = {"type": "snippet", "url": "", "text": ""}
            self.depth = 1

    def handle_data(self, data):
        if self.capture:
            self.capture["text"] += data

    def handle_endtag(self, tag):
        if not self.capture:
            return
        self.depth -= 1
        if self.depth <= 0:
            item = self.capture
            self.capture = None
            if item["type"] == "title" and item["text"].strip():
                url = item["url"]
                parsed = urlparse(url)
                if "duckduckgo.com" in parsed.netloc:
                    url = parse_qs(parsed.query).get("uddg", [url])[0]
                if url.startswith("//"):
                    url = "https:" + url
                if url.startswith("http://") or url.startswith("https://"):
                    self.results.append({"title": item["text"].strip(), "url": url, "snippet": ""})
            elif item["type"] == "snippet" and item["text"].strip():
                for result in reversed(self.results):
                    if not result["snippet"]:
                        result["snippet"] = item["text"].strip()
                        break


class GoogleResultsParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.results = []
        self.anchor = None
        self.title_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and self.anchor is None:
            self.anchor = {"url": attrs.get("href", ""), "title": ""}
        elif tag == "h3" and self.anchor is not None:
            self.title_depth += 1

    def handle_data(self, data):
        if self.anchor is not None and self.title_depth:
            self.anchor["title"] += data

    def handle_endtag(self, tag):
        if tag == "h3" and self.title_depth:
            self.title_depth -= 1
        elif tag == "a" and self.anchor is not None:
            url = self.anchor["url"]
            parsed = urlparse(url)
            if url.startswith("/url?"):
                url = parse_qs(parsed.query).get("q", [""])[0]
            if self.anchor["title"].strip() and url.startswith(("http://", "https://")):
                if "google.com" not in urlparse(url).netloc:
                    self.results.append({"title": self.anchor["title"].strip(), "url": url, "snippet": ""})
            self.anchor = None


class Handler(BaseHTTPRequestHandler):
    def send_json(self, value, status=200):
        content = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def read_body(self):
        length = min(int(self.headers.get("Content-Length", "0")), 20_000_000)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path == "/api/version":
            self.send_json({"appVersion": APP_VERSION})
            return
        if self.path == "/api/tts/status":
            package_exists = (PIPER_PACKAGES / "piper" / "__init__.py").is_file()
            model_exists = PIPER_MODEL.is_file() and PIPER_MODEL.with_suffix(PIPER_MODEL.suffix + ".json").is_file()
            self.send_json({"available": package_exists and model_exists,
                            "ready": PIPER_VOICE is not None,
                            "voice": "Amy (English, US)" if package_exists and model_exists else None,
                            "error": PIPER_ERROR})
            return
        if self.path == "/api/storage":
            self.send_json({
                "chats": read_json(CHATS_FILE, []),
                "memories": read_json(MEMORIES_FILE, []),
                "reviewState": read_json(STATE_FILE, {}),
            })
            return
        if self.path == "/api/openai/status":
            self.send_json({"configured": bool(load_openai_key())})
            return

        if self.path == "/api/cloudflare/status":
            self.send_json({
                "configured": bool(load_cloudflare_account_id() and load_cloudflare_token())
            })
            return

        
        # Serve the ARC VRM avatar model.
        if self.path == "/models/ARCVRM.vrm":
            model_file = APP_DIR / "models" / "ARCVRM.vrm"
            try:
                content = model_file.read_bytes()
            except OSError:
                self.send_error(404, "VRM model file not found")
                return

            self.send_response(200)
            self.send_header("Content-Type", "model/gltf-binary")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
            return

        if self.path not in ("/", "/ollama-chat.html"):
            self.send_error(404)
            return
        try:
            content = PAGE.read_bytes()
        except OSError as exc:
            self.send_error(500, str(exc))
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        try:
            data = self.read_body()
            if self.path == "/api/tts":
                text = str(data.get("text", "")).strip()[:1400]
                if not text:
                    self.send_json({"error": "Speech text is empty."}, 400)
                    return
                audio = synthesize_piper(text)
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(audio)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(audio)
                return
            if self.path == "/api/storage":
                chats = data.get("chats", [])
                memories = data.get("memories", [])
                if not isinstance(chats, list) or not isinstance(memories, list):
                    self.send_json({"error": "Chats and memories must be lists."}, 400)
                    return
                write_json(CHATS_FILE, chats)
                write_json(MEMORIES_FILE, memories)
                self.send_json({"ok": True})
                return
            if self.path == "/api/maintenance":
                self.review_old_history(data.get("model", ""))
                return
            if self.path == "/api/search":
                self.search_images = bool(data.get("images"))
                self.search_web(data.get("query", ""))
                return
            if self.path == "/api/correct-search":
                self.correct_search_query(data.get("query", ""), data.get("model", ""))
                return
            if self.path == "/api/openai/key":
                key = str(data.get("key", "")).strip()
                if not key:
                    try:
                        OPENAI_KEY_FILE.unlink()
                    except FileNotFoundError:
                        pass
                    self.send_json({"ok": True, "configured": False})
                    return
                if not key.startswith("sk-") or len(key) < 20:
                    self.send_json({"error": "That does not look like an OpenAI API key."}, 400)
                    return
                try:
                    protected = dpapi_protect(key.encode("utf-8"))
                except Exception as exc:
                    self.send_json({"error": "Could not secure the key with Windows user encryption: " + str(exc)}, 500)
                    return
                OPENAI_KEY_FILE.write_bytes(protected)
                self.send_json({"ok": True, "configured": True})
                return

                

            if self.path == "/api/cloudflare/key":
                account_id = str(data.get("accountId", "")).strip()
                token = str(data.get("token", "")).strip()

                if not account_id and not token:
                    for path in (CLOUDFLARE_ACCOUNT_FILE, CLOUDFLARE_TOKEN_FILE):
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                    self.send_json({"ok": True, "configured": False})
                    return

                try:
                    save_cloudflare_credentials(account_id, token)
                except Exception as exc:
                    self.send_json({"error": "Could not save Cloudflare credentials: " + str(exc)}, 500)
                    return

                self.send_json({"ok": True, "configured": True})
                return

            if self.path == "/api/cloudflare/image":
                self.cloudflare_image(data)
                return

            if self.path == "/api/openai/fallback":
                self.openai_fallback(data)
                return
            self.send_error(404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def openai_fallback(self, data):
        key = load_openai_key()
        if not key:
            self.send_json({"error": "OpenAI fallback is not configured. Add an API key in Arc Settings."}, 400)
            return
        mode = "image" if data.get("mode") == "image" else "text"
        prompt = str(data.get("prompt", "")).strip()[:12000]
        if not prompt:
            self.send_json({"error": "The fallback request was empty."}, 400)
            return
        if mode == "image":
            req_data = json.dumps({"model": "gpt-image-1", "prompt": prompt,
                                   "size": "1024x1024", "quality": "low", "n": 1}).encode("utf-8")
            request = urllib.request.Request("https://api.openai.com/v1/images/generations", data=req_data,
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    result = json.loads(response.read().decode("utf-8", errors="replace"))
                image = result.get("data", [{}])[0].get("b64_json")
                if not image:
                    raise ValueError("The image service returned no image data.")
                self.send_json({"image": image, "mimeType": "image/png", "text": "I created this image for you."})
            except Exception as exc:
                self.send_json({"error": openai_error(exc)}, 502)
            return
        messages = data.get("messages", [])
        clean = []
        if isinstance(messages, list):
            for message in messages[-8:]:
                if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
                    continue
                content = str(message.get("content", ""))[:5000]
                if content:
                    clean.append({"role": message["role"], "content": content})
        system = str(data.get("system", "You are Arc, a helpful assistant. Give a brief summary by default; expand only when asked for more detail."))[:8000]
        req_data = json.dumps({"model": "gpt-4.1-mini", "messages": [{"role": "system", "content": system}, *clean],
                               "max_tokens": 1600, "temperature": 0.3}).encode("utf-8")
        request = urllib.request.Request("https://api.openai.com/v1/chat/completions", data=req_data,
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.loads(response.read().decode("utf-8", errors="replace"))
            text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            self.send_json({"text": text, "model": "gpt-4.1-mini"})
        except Exception as exc:
            self.send_json({"error": openai_error(exc)}, 502)

            

    def cloudflare_image(self, data):
        account_id = load_cloudflare_account_id()
        token = load_cloudflare_token()

        if not account_id or not token:
            self.send_json({
                "error": "Cloudflare is not configured. Add your Account ID and API token in Arc Settings."
            }, 400)
            return

        prompt = str(data.get("prompt", "")).strip()[:12000]
        if not prompt:
            self.send_json({"error": "The image prompt is empty."}, 400)
            return

        payload = json.dumps({
            "prompt": prompt,
            "steps": 4
        }).encode("utf-8")

        url = (
            "https://api.cloudflare.com/client/v4/accounts/"
            + quote_plus(account_id)
            + "/ai/run/@cf/black-forest-labs/flux-1-schnell"
        )

        request = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                result = json.loads(response.read().decode("utf-8", errors="replace"))

            image = result.get("result", {}).get("image")
            if not image:
                raise ValueError("Cloudflare returned no image data.")

            self.send_json({
                "image": image,
                "mimeType": "image/png",
                "text": "I created this image for you."
            })

        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8", errors="replace"))
                errors = detail.get("errors", [])
                message = errors[0].get("message", "") if errors else ""
            except Exception:
                message = ""

            self.send_json({
                "error": message or "Cloudflare image generation failed (HTTP "
                         + str(exc.code) + "). Check your credentials and Cloudflare limits."
            }, 502)

        except Exception as exc:
            self.send_json({
                "error": "Cloudflare image generation failed: " + str(exc)
            }, 502)

    def correct_search_query(self, query, model):
        query = str(query).strip()[:300]
        model = str(model).strip()[:120]
        if not query or not model:
            self.send_json({"query": query})
            return
        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": (
                    "Correct only clear spelling mistakes in this short English search query. "
                    "Preserve meaning, word order, names, and correctly spelled technical terms. "
                    "If unsure, leave the word unchanged. Output only the corrected query; do not answer it."
                )},
                {"role": "user", "content": query},
            ],
            "stream": False,
            "keep_alive": "30m",
            "options": {"temperature": 0, "num_predict": 64},
        }).encode("utf-8")
        request = urllib.request.Request(
            "http://localhost:11434/api/chat", data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8", errors="replace"))
            corrected = str(result.get("message", {}).get("content", "")).strip().strip("\"'“” \n\r\t")
            if re.search(r"\barrowplane\b", query, re.I) and re.fullmatch(r"arrow[ -]plane", corrected, re.I):
                corrected = re.sub(r"arrow[ -]plane", "airplane", corrected, flags=re.I)
            corrected = re.sub(r"\bflys\b", "flies", corrected, flags=re.I)
            if not corrected or len(corrected) > 300 or "\n" in corrected:
                corrected = query
            self.send_json({"query": corrected})
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            self.send_json({"query": query})

    def search_web(self, query):
        query = str(query).strip()[:300]
        if not query:
            self.send_json({"results": [], "googleResults": [], "images": []})
            return
        image_request = self._is_image_request()
        # Image requests need actual thumbnail data; don't let slower web text
        # scrapers occupy their short response window or delay the picture grid.
        ddg_task = SEARCH_POOL.submit(self.fetch_duckduckgo, query) if not image_request else None
        google_task = SEARCH_POOL.submit(self.fetch_google, query) if not image_request else None
        image_tasks = [SEARCH_POOL.submit(self.fetch_duckduckgo_images, query), SEARCH_POOL.submit(self.fetch_commons_images, query)] if image_request else []
        tasks = [task for task in (ddg_task, google_task) if task] + image_tasks
        done, pending = wait(tasks, timeout=4.5 if image_request else 1.8)
        ddg = ddg_task.result() if ddg_task and ddg_task in done else []
        google = google_task.result() if google_task and google_task in done else []
        images = []
        for task in image_tasks:
            if task in done:
                try:
                    images.extend(task.result())
                except Exception:
                    pass
        unique_images = []
        seen_images = set()
        for image in images:
            key = image.get("thumbnail") or image.get("pageUrl")
            if key and key not in seen_images:
                seen_images.add(key)
                unique_images.append(image)
            if len(unique_images) >= 12:
                break
        images = unique_images
        for task in pending:
            task.cancel()
        self.send_json({
            "results": ddg,
            "googleResults": google,
            "images": images,
            "searchLinks": {
                "duckduckgo": "https://duckduckgo.com/?q=" + quote_plus(query),
                "google": "https://www.google.com/search?q=" + quote_plus(query),
                "duckduckgoImages": "https://duckduckgo.com/?q=" + quote_plus(query) + "&iax=images&ia=images",
                "googleImages": "https://www.google.com/search?tbm=isch&q=" + quote_plus(query),
            },
        })

    def _is_image_request(self):
        # The browser sends the cleaned query; use the original prompt flag for image search.
        return getattr(self, "search_images", False)

    def fetch_duckduckgo(self, query):
        url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "text/html",
        })
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                page = response.read(2_000_000).decode("utf-8", errors="replace")
            parser = SearchResultsParser()
            parser.feed(page)
            return parser.results[:5]
        except (urllib.error.URLError, TimeoutError):
            return []

    def fetch_google(self, query):
        url = "https://www.google.com/search?hl=en&num=8&q=" + quote_plus(query)
        request = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "text/html",
        })
        try:
            with urllib.request.urlopen(request, timeout=4) as response:
                page = response.read(2_000_000).decode("utf-8", errors="replace")
            parser = GoogleResultsParser()
            parser.feed(page)
            return parser.results[:5]
        except (urllib.error.URLError, TimeoutError):
            return []

    def fetch_duckduckgo_images(self, query):
        """Fetch real DuckDuckGo image results with source and thumbnail URLs."""
        base = "https://duckduckgo.com/"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
        try:
            landing = urllib.request.Request(base + "?q=" + quote_plus(query) + "&iax=images&ia=images", headers=headers)
            with urllib.request.urlopen(landing, timeout=4) as response:
                html = response.read(1_500_000).decode("utf-8", errors="replace")
            match = re.search(r"vqd=([\"'])(.*?)\1", html)
            if not match:
                match = re.search(r"vqd=([^&\"']+)", html)
            if not match:
                return []
            vqd = match.group(2) if match.lastindex and match.lastindex > 1 else match.group(1)
            params = urlencode({"l": "us-en", "o": "json", "q": query, "vqd": vqd, "f": ",,,", "p": "1"})
            request = urllib.request.Request(base + "i.js?" + params, headers={**headers, "Referer": base + "?q=" + quote_plus(query) + "&iax=images&ia=images"})
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read(3_000_000).decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, ValueError):
            return []
        output = []
        for result in payload.get("results", []):
            thumb = result.get("thumbnail") or result.get("image")
            image = result.get("image")
            page = result.get("url")
            if not thumb or not image or not page or not all(str(url).startswith("https://") for url in (thumb, image, page)):
                continue
            output.append({
                "title": str(result.get("title") or "Image result")[:240],
                "thumbnail": thumb,
                "pageUrl": page,
                "license": "DuckDuckGo image result",
                "artist": str(result.get("source") or urlparse(page).netloc)[:120],
            })
            if len(output) >= 8:
                break
        return output

    def fetch_commons_images(self, query):
        # Split conjunctions into distinct subjects, so phrases like "pigs and flies"
        # produce pictures of both subjects instead of the idiom "pigs might fly".
        parts = [part.strip() for part in re.split(r"\s+and\s+", query, flags=re.I) if part.strip()]
        if len(parts) > 1:
            groups = [self._fetch_commons_subject(part) for part in parts[:3]]
            output = []
            seen = set()
            for index in range(max((len(group) for group in groups), default=0)):
                for group in groups:
                    if index < len(group):
                        item = group[index]
                        key = item.get("thumbnail") or item.get("pageUrl")
                        if key and key not in seen:
                            seen.add(key)
                            output.append(item)
                        if len(output) >= 8:
                            return output
            if output:
                return output
        return self._fetch_commons_subject(query)

    def _fetch_commons_subject(self, query):
        # Bias common produce names toward the fruit rather than brands or namesakes.
        if query.strip().casefold() in {"apple", "banana", "orange", "pear", "mango", "grape"}:
            query = query.strip() + " fruit"
        params = urlencode({
            "action": "query", "generator": "search", "gsrnamespace": "6",
            "gsrsearch": query, "gsrlimit": "8", "prop": "imageinfo",
            "iiprop": "url", "iiurlwidth": "520", "format": "json",
        })
        request = urllib.request.Request(
            "https://commons.wikimedia.org/w/api.php?" + params,
            headers={"User-Agent": "ArcLocalChat/3.6.0 (local Ollama chat app)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read(2_000_000).decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return []
        output = []
        for page in payload.get("query", {}).get("pages", {}).values():
            info = (page.get("imageinfo") or [{}])[0]
            thumb = info.get("thumburl") or info.get("url")
            if not thumb or not thumb.startswith("https://"):
                continue
            output.append({
                "title": page.get("title", "Image").removeprefix("File:"),
                "thumbnail": thumb,
                "pageUrl": info.get("descriptionurl", "https://commons.wikimedia.org/"),
                "license": "Wikimedia Commons",
                "artist": "",
            })
            if len(output) == 8:
                break
        return output

    def review_old_history(self, model):
        now = datetime.now(timezone.utc)
        state = read_json(STATE_FILE, {})
        last = state.get("lastReview")
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if now - last_dt < timedelta(days=14):
                    self.send_json({"reviewed": False, "reason": "not_due"})
                    return
            except ValueError:
                pass

        chats = read_json(CHATS_FILE, [])
        memories = read_json(MEMORIES_FILE, [])
        cutoff = now - timedelta(days=14)
        old_chats = []
        recent_chats = []
        for chat in chats:
            try:
                created = datetime.fromisoformat(chat.get("createdAt", "").replace("Z", "+00:00"))
            except (ValueError, TypeError):
                created = now
                chat["createdAt"] = now.isoformat()
            (old_chats if created < cutoff else recent_chats).append(chat)

        if not old_chats:
            write_json(CHATS_FILE, chats)
            write_json(STATE_FILE, {"lastReview": now.isoformat()})
            self.send_json({"reviewed": True, "kept": 0, "memories": memories})
            return
        if not model:
            self.send_json({"error": "Choose an Ollama model before Arc can review older chats."}, 400)
            return

        review_input = [{
            "id": chat.get("id"),
            "title": chat.get("title", "Untitled"),
            "messages": [{"role": m.get("role"), "content": str(m.get("content", ""))[:2500]}
                         for m in chat.get("messages", [])[-30:]],
        } for chat in old_chats]
        prompt = (
            "Review these conversations for long-term usefulness to this same user. "
            "Keep full conversations only if they contain important unresolved work, ongoing projects, "
            "or information the user is likely to need again. Save concise memories for stable preferences, "
            "personal context the user explicitly shared, recurring goals, and useful project facts. "
            "Do not preserve trivial one-off questions, sensitive secrets, or guesses. Existing memories "
            "are included; update or remove stale ones. Return only JSON with exactly these keys: "
            "keep_ids (array of conversation IDs to retain) and memories (array of concise strings).\n\n"
            "Existing memories:\n" + json.dumps(memories, ensure_ascii=False) +
            "\n\nOlder conversations:\n" + json.dumps(review_input, ensure_ascii=False)
        )
        req_data = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": "You are Arc's private memory curator. Follow the requested JSON schema exactly."},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": "json",
            "keep_alive": "10m",
            "options": {"temperature": 0, "num_predict": 2048},
        }).encode("utf-8")
        request = urllib.request.Request(
            "http://localhost:11434/api/chat", data=req_data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            self.send_json({"error": "Ollama could not review older chats: " + str(exc)}, 502)
            return
        try:
            decision = json.loads(result.get("message", {}).get("content", "{}"))
            keep_ids = set(str(x) for x in decision.get("keep_ids", []))
            new_memories = decision.get("memories", [])
            if not isinstance(new_memories, list) or not all(isinstance(x, str) for x in new_memories):
                raise ValueError("Ollama returned an invalid memories list.")
        except (json.JSONDecodeError, AttributeError, ValueError) as exc:
            self.send_json({"error": "Ollama returned an invalid memory review: " + str(exc)}, 502)
            return

        kept_old = [chat for chat in old_chats if str(chat.get("id")) in keep_ids]
        write_json(CHATS_FILE, recent_chats + kept_old)
        write_json(MEMORIES_FILE, new_memories)
        write_json(STATE_FILE, {"lastReview": now.isoformat()})
        self.send_json({"reviewed": True, "kept": len(kept_old), "memories": new_memories})

    def log_message(self, *_args):
        pass


def main():
    if not PAGE.is_file():
        raise SystemExit("Cannot find ollama-chat.html next to this launcher.")
    try:
        server = ThreadingHTTPServer(ADDRESS, Handler)
    except OSError:
        webbrowser.open("http://localhost:8766/")
        return
    threading.Thread(target=warm_piper, name="Arc-Piper-warmup", daemon=True).start()
    threading.Timer(0.4, lambda: webbrowser.open("http://localhost:8766/")).start()
    server.serve_forever()


def warm_piper():
    try:
        get_piper_voice()
    except Exception:
        pass


if __name__ == "__main__":
    main()
