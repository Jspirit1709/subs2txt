"""
Subs2Txt - App Kivy para Android.
Descarga subtitulos de YouTube (y texto de paginas web) y los junta en un .txt
con indice, titulo real y palabras clave.
"""
import json
import os
import re
import threading
import time
import urllib.request
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from kivy.app import App
from kivy.clock import Clock
from kivy.core.window import Window
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.uix.textinput import TextInput
from kivy.utils import platform

LANGS = ["es", "en"]
PAUSA = 2
SALIDA = "transcripciones.txt"

STOP = set("""
para pero como esto esta este estos estas entonces porque cuando donde desde hasta sobre
entre bien vamos tengo tenemos tiene tienen puede pueden hacer hace vemos digamos
ahora aqui ahi luego despues ejemplo video bueno cada todo todos todas mismo misma
that this with from have what when where then there which will your about because
just they them into also very more some like going gonna actually
""".split())

URL_RE = re.compile(
    r"https?://[^\s,;<>\"'()]+"
    r"|(?:www\.|m\.|music\.)?(?:youtube\.com|youtu\.be)/[^\s,;<>\"'()]+",
    re.IGNORECASE)

NO_DISPONIBLE = ("private video", "video unavailable", "this video is not available",
                 "members-only", "removed by the uploader", "has been terminated",
                 "account associated", "sign in to confirm your age",
                 "no longer available", "copyright")

LOCK = threading.Lock()


# ---------------------------------------------------------------- carpetas
def carpeta_salida():
    """Devuelve (carpeta_de_salida, carpeta_de_trabajo)."""
    candidatos = []
    if platform == "android":
        try:
            from android.storage import primary_external_storage_path, app_storage_path
            candidatos.append(Path(primary_external_storage_path()) / "Download")
            candidatos.append(Path(app_storage_path()))
        except Exception:
            pass
    candidatos.append(Path.home())
    for c in candidatos:
        try:
            c.mkdir(parents=True, exist_ok=True)
            test = c / ".w_test"
            test.write_text("x")
            test.unlink()
            work = c / "subs_work"
            work.mkdir(parents=True, exist_ok=True)
            return c, work
        except Exception:
            continue
    c = Path(".")
    (c / "subs_work").mkdir(exist_ok=True)
    return c, c / "subs_work"


# ---------------------------------------------------------------- utilidades
class Callado:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def video_id(url):
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})", url)
    return m.group(1) if m else None


def es_web(url):
    return not re.search(r"youtube\.com|youtu\.be", url, re.IGNORECASE)


def extraer_urls(texto):
    out = []
    for m in URL_RE.finditer(texto):
        u = m.group(0).rstrip(".,)")
        if not u.lower().startswith("http"):
            u = "https://" + u
        if u not in out:
            out.append(u)
    return out


def palabras_clave(texto, n=6):
    words = re.findall(r"[a-záéíóúñü]{5,}", texto.lower())
    c = Counter(w for w in words if w not in STOP)
    return [w for w, _ in c.most_common(n)]


def limpiar_vtt(path):
    out, prev = [], None
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if (not line or "-->" in line
                    or line.startswith(("WEBVTT", "Kind:", "Language:"))):
                continue
            line = re.sub(r"<[^>]*>", "", line).strip()
            if line and line != prev:
                out.append(line)
                prev = line
    return "\n".join(out)


class ExtractorTexto(HTMLParser):
    SALTAR = {"script", "style", "nav", "header", "footer", "aside",
              "noscript", "svg", "template"}
    BLOQUES = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote",
               "td", "th", "pre", "div", "br", "section", "tr"}
    TITULOS = {"h1", "h2", "h3", "h4"}

    def __init__(self):
        super().__init__()
        self.skip = 0
        self.art = 0
        self.en_title = False
        self.titulo = ""
        self.h = False
        self.buf = []
        self.todo = []
        self.articulo = []

    def _flush(self):
        t = " ".join("".join(self.buf).split())
        self.buf = []
        if not t:
            return
        if not self.h and len(t) < 25:
            return
        if self.h:
            t = "\n## " + t
        self.todo.append(t)
        if self.art:
            self.articulo.append(t)

    def handle_starttag(self, tag, attrs):
        if tag in self.SALTAR:
            self.skip += 1
            return
        if tag == "title":
            self.en_title = True
        if tag in self.BLOQUES or tag in ("article", "main"):
            self._flush()
        if tag in ("article", "main"):
            self.art += 1
        if tag in self.TITULOS:
            self.h = True

    def handle_endtag(self, tag):
        if tag in self.SALTAR:
            self.skip = max(0, self.skip - 1)
            return
        if tag == "title":
            self.en_title = False
        if tag in self.BLOQUES or tag in ("article", "main"):
            self._flush()
        if tag in ("article", "main"):
            self.art = max(0, self.art - 1)
        if tag in self.TITULOS:
            self.h = False

    def handle_data(self, d):
        if self.en_title:
            self.titulo += d
            return
        if self.skip:
            return
        self.buf.append(d)

    def resultado(self):
        self._flush()
        usar = self.articulo if sum(len(x) for x in self.articulo) > 800 else self.todo
        vistos, out = set(), []
        for l in usar:
            if l not in vistos:
                vistos.add(l)
                out.append(l)
        return "\n".join(out)


# ---------------------------------------------------------------- motor
class Motor:
    def __init__(self, log, salida_dir, work_dir):
        self.log = log
        self.downloads = salida_dir
        self.work = work_dir
        self.titles_file = work_dir / "titles.json"
        self.cancelar = False

    def cargar_titulos(self):
        try:
            with open(self.titles_file, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def guardar_titulos(self, t):
        with LOCK:
            with open(self.titles_file, "w", encoding="utf-8") as f:
                json.dump(t, f, ensure_ascii=False, indent=1)

    def archivos_de(self, vid, lang):
        return [f for f in os.listdir(self.work)
                if "[%s]" % vid in f and f.endswith(".%s.vtt" % lang)]

    def expandir(self, urls):
        from yt_dlp import YoutubeDL
        out = []
        for u in urls:
            if "playlist?list=" in u or ("&list=" in u and "watch?v=" not in u):
                self.log("Lista detectada, obteniendo videos...")
                try:
                    opts = {"extract_flat": True, "quiet": True,
                            "no_warnings": True, "logger": Callado()}
                    with YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(u, download=False)
                    for e in info.get("entries", []):
                        if e and e.get("id"):
                            out.append("https://www.youtube.com/watch?v=" + e["id"])
                except Exception as ex:
                    self.log("  No pude leer la lista: " + str(ex)[:100])
            else:
                out.append(u)
        return out

    def descargar(self, vid, titulos):
        from yt_dlp import YoutubeDL
        url = "https://www.youtube.com/watch?v=" + vid
        for lang in LANGS:
            opts = {
                "skip_download": True,
                "noplaylist": True,
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [lang],
                "outtmpl": str(self.work / "%(title).60s [%(id)s].%(ext)s"),
                "windowsfilenames": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "sleep_interval_requests": 1,
                "sleep_interval_subtitles": 2,
                "logger": Callado(),
            }
            error = ""
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                if info and info.get("title"):
                    titulos[vid] = {"title": info["title"],
                                    "channel": info.get("uploader", "")}
                    self.guardar_titulos(titulos)
            except Exception as ex:
                error = str(ex)
            if self.archivos_de(vid, lang):
                return True
            low = error.lower()
            if any(k in low for k in NO_DISPONIBLE):
                self.log("  [%s] privado o no disponible" % vid)
                return False
            if "429" in low or "too many" in low:
                self.log("  [%s] limite de YouTube, lo salto" % vid)
                return False
        return False

    def descargar_web(self, url):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36",
                "Accept-Language": "es,en;q=0.8"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                raw = resp.read()
                cs = resp.headers.get_content_charset() or "utf-8"
            html = raw.decode(cs, errors="ignore")
        except Exception as ex:
            self.log("  error: " + str(ex)[:100])
            return None
        p = ExtractorTexto()
        try:
            p.feed(html)
        except Exception:
            pass
        texto = p.resultado()
        if len(texto) < 200:
            self.log("  casi sin texto (JavaScript o bloqueo)")
            return None
        dominio = urlparse(url).netloc
        titulo = " ".join(p.titulo.split()) or dominio
        return {"id": url, "titulo": titulo, "canal": dominio, "lang": "web",
                "url": url, "texto": texto, "claves": palabras_clave(texto)}

    def unir(self, titulos, ids, webs):
        por_id = {}
        for f in sorted(os.listdir(self.work)):
            m = re.search(r"\[([A-Za-z0-9_-]{11})\]\.(es|en)\.vtt$", f)
            if not m:
                continue
            vid, lang = m.group(1), m.group(2)
            if vid not in ids:
                continue
            if vid not in por_id or LANGS.index(lang) < LANGS.index(por_id[vid][1]):
                por_id[vid] = (f, lang)

        bloques = []
        for vid, (f, lang) in por_id.items():
            texto = limpiar_vtt(self.work / f)
            t = titulos.get(vid, {})
            titulo = t.get("title") or re.sub(r"\s*\[[^\]]+\]\.\w+\.vtt$", "", f)
            bloques.append({"id": vid, "titulo": titulo,
                            "canal": t.get("channel", ""), "lang": lang,
                            "texto": texto,
                            "url": "https://www.youtube.com/watch?v=" + vid,
                            "claves": palabras_clave(texto)})
        bloques.extend(webs)
        bloques.sort(key=lambda b: b["titulo"].lower())

        ruta = self.downloads / SALIDA
        with open(ruta, "w", encoding="utf-8") as out:
            out.write("INDICE\n")
            for i, b in enumerate(bloques, 1):
                out.write("%d. %s\n" % (i, b["titulo"]))
            out.write("\n" + "=" * 60 + "\n\n")
            for i, b in enumerate(bloques, 1):
                out.write("%d. %s\n" % (i, b["titulo"].upper()))
                if b["canal"]:
                    out.write("Canal: %s\n" % b["canal"])
                out.write("Idioma: %s | %s\n" % (b["lang"], b["url"]))
                out.write("Temas: %s\n\n" % ", ".join(b["claves"]))
                out.write(b["texto"] + "\n\n" + "-" * 60 + "\n\n")
        return ruta

    def ejecutar(self, texto_links):
        urls = extraer_urls(texto_links)
        if not urls:
            self.log("No hay links validos.")
            return None
        urls = self.expandir(urls)
        self.log("Total de enlaces: %d" % len(urls))
        titulos = self.cargar_titulos()
        faltan, webs, ids = [], [], set()

        for i, url in enumerate(urls, 1):
            if self.cancelar:
                self.log("Cancelado.")
                break
            if es_web(url):
                self.log("[web] " + url)
                b = self.descargar_web(url)
                if b:
                    webs.append(b)
                    self.log("  listo")
                else:
                    faltan.append(url)
                time.sleep(1)
                continue
            vid = video_id(url)
            if not vid:
                self.log("[%d/%d] link no valido: %s" % (i, len(urls), url))
                continue
            ids.add(vid)
            if any(self.archivos_de(vid, l) for l in LANGS) and vid in titulos:
                self.log("[%d/%d] %s ya descargado, salto" % (i, len(urls), vid))
                continue
            ok = self.descargar(vid, titulos)
            self.log("[%d/%d] %s %s" % (i, len(urls), vid,
                                        "listo" if ok else "sin subtitulos"))
            if not ok:
                faltan.append(url)
            time.sleep(PAUSA)

        ruta = self.unir(titulos, ids, webs)
        self.log("\nListo: %s" % ruta)
        if faltan:
            self.log("Sin subtitulos / fallaron:")
            for u in faltan:
                self.log("  " + u)
        return ruta


# ---------------------------------------------------------------- interfaz
class Subs2TxtApp(App):
    title = "Subs2Txt"

    def build(self):
        Window.clearcolor = (0.08, 0.08, 0.1, 1)
        if platform == "android":
            try:
                from android.permissions import request_permissions, Permission
                request_permissions([Permission.WRITE_EXTERNAL_STORAGE,
                                     Permission.READ_EXTERNAL_STORAGE])
            except Exception:
                pass

        root = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(8))

        root.add_widget(Label(
            text="Pega tus links (YouTube, listas o webs)",
            size_hint_y=None, height=dp(30), font_size="16sp"))

        self.entrada = TextInput(
            hint_text="https://youtu.be/...\nhttps://blog...",
            multiline=True, size_hint_y=0.35)
        root.add_widget(self.entrada)

        botones = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        self.btn_pegar = Button(text="Pegar")
        self.btn_pegar.bind(on_release=self.pegar)
        self.btn_iniciar = Button(text="Iniciar", background_color=(0.2, 0.6, 0.3, 1))
        self.btn_iniciar.bind(on_release=self.iniciar)
        self.btn_cancelar = Button(text="Cancelar", disabled=True)
        self.btn_cancelar.bind(on_release=self.cancelar)
        botones.add_widget(self.btn_pegar)
        botones.add_widget(self.btn_iniciar)
        botones.add_widget(self.btn_cancelar)
        root.add_widget(botones)

        self.sv = ScrollView()
        self.log_lbl = Label(text="", size_hint_y=None, halign="left",
                             valign="top", font_size="13sp")
        self.log_lbl.bind(
            width=lambda w, v: setattr(w, "text_size", (v, None)),
            texture_size=lambda w, v: setattr(w, "height", v[1]))
        self.sv.add_widget(self.log_lbl)
        root.add_widget(self.sv)
        self.motor = None
        return root

    def pegar(self, *_):
        from kivy.core.clipboard import Clipboard
        txt = Clipboard.paste() or ""
        self.entrada.text = (self.entrada.text + "\n" + txt).strip()

    def log(self, msg):
        Clock.schedule_once(lambda dt: self._log(msg))

    def _log(self, msg):
        self.log_lbl.text += msg + "\n"
        Clock.schedule_once(lambda dt: setattr(self.sv, "scroll_y", 0), 0.05)

    def iniciar(self, *_):
        texto = self.entrada.text
        if not extraer_urls(texto):
            self._log("No hay links validos.")
            return
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            self._log("Falta yt-dlp en la app.")
            return
        salida, work = carpeta_salida()
        self._log("Carpeta de salida: %s" % salida)
        self.motor = Motor(self.log, salida, work)
        self.btn_iniciar.disabled = True
        self.btn_cancelar.disabled = False
        threading.Thread(target=self._hilo, args=(texto,), daemon=True).start()

    def _hilo(self, texto):
        try:
            self.motor.ejecutar(texto)
        except Exception as ex:
            import traceback
            self.log("Error:\n" + traceback.format_exc()[-600:])
        finally:
            Clock.schedule_once(self._fin)

    def _fin(self, dt):
        self.btn_iniciar.disabled = False
        self.btn_cancelar.disabled = True

    def cancelar(self, *_):
        if self.motor:
            self.motor.cancelar = True
            self._log("Cancelando al terminar el video actual...")


if __name__ == "__main__":
    Subs2TxtApp().run()
