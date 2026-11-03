#!/usr/bin/env python3
"""Malayalam handwriting / voice pad for Linux (X11).

A small always-on-top window that never takes keyboard focus. Words you write or
speak are pasted into whichever window you were typing in (for example your browser).
It runs the same web app as the phone version, served from this folder's parent.

A tray icon toggles the window (left click) or offers Show/Hide and Quit (right click).
Closing the window only hides it. Start it with --hidden to begin in the tray only,
which is what the startup entry does. Starting a second copy just shows the first.
"""
import functools
import http.server
import json
import os
import secrets
import shutil
import signal
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402

try:
    gi.require_version("XApp", "1.0")
    from gi.repository import XApp  # noqa: E402
except (ValueError, ImportError):
    XApp = None
from Xlib import X, XK, display, protocol  # noqa: E402
from Xlib.ext import xtest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ICON_IDLE = os.path.join(ROOT, "icon-512.png")
ICON_REC = [os.path.join(ROOT, f"icon-recording-{i}.png") for i in range(4)]   # red, pulsing: the microphone is on
PULSE_MS = 300
PORT = 18765
SERVED = {"/", "/index.html", "/manifest.json", "/sw.js", "/icon-180.png", "/icon-512.png", "/stt.html"}
DATA_DIR = os.path.expanduser("~/.local/share/ml-handwriting")
LOG_FILE = os.path.expanduser("~/.cache/ml-handwriting/debug.log")
CACHE_DIR = os.path.expanduser("~/.cache/ml-handwriting")
CHROME_DIR = os.path.expanduser("~/.local/share/ml-handwriting/chrome-speech")


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves only the web app's own files, never the rest of the folder (such as .git)."""

    on_show = None   # set by the app: called when a second copy asks the first one to show itself
    on_stt = None    # set by the app: called with what the hidden Chrome speech helper reports
    stt_token = ""   # secret handed only to the helper, so other pages on this computer cannot inject text

    def do_POST(self):
        if self.path != "/__stt" or not Handler.stt_token or self.headers.get("X-ML-Token") != Handler.stt_token:
            self.send_error(403)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self.send_error(400)
            return
        if Handler.on_stt and isinstance(body, dict):
            GLib.idle_add(Handler.on_stt, body)
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        if self.path == "/__show":
            if Handler.on_show:
                GLib.idle_add(Handler.on_show)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path.split("?")[0] not in SERVED:
            self.send_error(404)
            return
        super().do_GET()

    def log_message(self, *args):
        pass


def start_server():
    handler = functools.partial(Handler, directory=ROOT)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


class Typer:
    """Pastes text and presses keys in the focused window through the clipboard and XTest."""

    def __init__(self):
        self.disp = display.Display()
        self.clip = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        self.queue = []
        self.busy = False

    def _code(self, name):
        return self.disp.keysym_to_keycode(XK.string_to_keysym(name))

    def _tap(self, name, mods=()):
        for m in mods:
            xtest.fake_input(self.disp, X.KeyPress, self._code(m))
        code = self._code(name)
        xtest.fake_input(self.disp, X.KeyPress, code)
        xtest.fake_input(self.disp, X.KeyRelease, code)
        for m in reversed(mods):
            xtest.fake_input(self.disp, X.KeyRelease, self._code(m))
        self.disp.sync()

    def active_window(self):
        prop = self.disp.screen().root.get_full_property(self.disp.intern_atom("_NET_ACTIVE_WINDOW"), X.AnyPropertyType)
        return int(prop.value[0]) if prop and len(prop.value) and prop.value[0] else 0

    def activate(self, wid):
        """Give keyboard focus back to the window the user was typing in."""
        if not wid:
            return
        ev = protocol.event.ClientMessage(
            window=self.disp.create_resource_object("window", wid),
            client_type=self.disp.intern_atom("_NET_ACTIVE_WINDOW"),
            data=(32, [2, X.CurrentTime, 0, 0, 0]),   # 2 = request from a pager/taskbar, which window managers obey
        )
        self.disp.screen().root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self.disp.flush()

    def push(self, msg):
        if "text" in msg:
            # Spaces at the start or end are typed as real Space key presses: many web editors
            # trim spaces from pasted text, which would glue the next phrase to this one.
            text = str(msg["text"])
            core = text.strip(" ")
            if core:
                lead, trail = len(text) - len(text.lstrip(" ")), len(text) - len(text.rstrip(" "))
            else:
                lead, trail = len(text), 0
            self.queue.extend([{"key": "space"}] * lead)
            if core:
                self.queue.append({"paste": core})
            self.queue.extend([{"key": "space"}] * trail)
        else:
            self.queue.append(msg)
        self._pump()

    def _pump(self):
        if self.busy or not self.queue:
            return
        self.busy = True
        msg = self.queue.pop(0)
        if "paste" in msg:
            self.clip.set_text(msg["paste"], -1)
            GLib.timeout_add(60, self._paste)
        elif "back" in msg:
            for _ in range(int(msg["back"])):
                self._tap("BackSpace")
            GLib.timeout_add(30, self._done)
        elif "key" in msg:
            self._tap(str(msg["key"]))
            GLib.timeout_add(30, self._done)
        else:
            self._done()

    def _paste(self):
        self._tap("v", ("Control_L",))
        GLib.timeout_add(80, self._done)
        return False

    def _done(self):
        self.busy = False
        self._pump()
        return False


class ChromeSpeech:
    """Runs Chrome hidden with a small page that uses Google's speech recognition (the engine behind
    Chrome and Android voice typing) and reports the words back to this app."""

    def __init__(self):
        self.proc = None
        self.token = secrets.token_urlsafe(16)

    @staticmethod
    def find_chrome():
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            path = shutil.which(name)
            if path:
                return path
        return None

    def start(self, lang):
        """Returns None when started, or a short message saying why not."""
        self.stop()
        chrome = self.find_chrome()
        if not chrome:
            return "Google Chrome is not installed, so this engine is not available."
        os.makedirs(CHROME_DIR, exist_ok=True)
        url = f"http://127.0.0.1:{PORT}/stt.html?t={self.token}&lang={lang}"
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             "--use-fake-ui-for-media-stream", f"--user-data-dir={CHROME_DIR}", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return None

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def stop(self):
        if self.proc is not None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)   # Chrome starts several processes
            except (ProcessLookupError, PermissionError):
                pass
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            self.proc = None


class App:
    def __init__(self, hidden=False):
        self.typer = Typer()
        self.last_real = 0     # the last window you were using (not this app, not the panel)
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        open(LOG_FILE, "w").close()   # the log only covers the current run
        manager = WebKit2.WebsiteDataManager(base_data_directory=DATA_DIR, base_cache_directory=CACHE_DIR)
        ctx = WebKit2.WebContext.new_with_website_data_manager(manager)
        ucm = WebKit2.UserContentManager()
        ucm.register_script_message_handler("mlbridge")
        ucm.connect("script-message-received::mlbridge", self.on_message)
        self.view = WebKit2.WebView(web_context=ctx, user_content_manager=ucm)
        settings = self.view.get_settings()
        settings.set_property("enable-media-stream", True)
        settings.set_property("enable-mediasource", True)
        settings.set_property("media-playback-requires-user-gesture", False)   # the tray menu starts the microphone, not a click in the page
        self.view.connect("permission-request", self.on_permission)
        self.view.load_uri(f"http://localhost:{PORT}/")

        win = Gtk.Window(title="Malayalam Handwriting")
        win.set_default_size(430, 660)   # roomy text box; the writing pad is secondary here
        win.set_accept_focus(False)        # clicks here must not steal focus from the window you type in
        win.set_focus_on_map(False)
        win.set_keep_above(True)
        win.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        try:
            win.set_icon_from_file(os.path.join(ROOT, "icon-512.png"))
        except GLib.Error:
            pass
        win.connect("destroy", Gtk.main_quit)
        win.connect("delete-event", self.on_close)
        self.win = win
        self.ghosted = False
        self.previous = 0
        self.focus_wanted = False   # the page wants keyboard focus (settings dialog, editing the box, "This box only")
        self.has_focus_mode = False # this window currently holds the keyboard
        self.held = []              # words waiting while focus is handed back to the other window
        self.releasing = False
        self.speech = ChromeSpeech()
        self.speech_wanted = False
        self.speech_idle = 0
        Handler.stt_token = self.speech.token
        Handler.on_stt = self.on_speech
        win.add(self.view)
        win.show_all()
        self.tray = self.make_tray()
        Handler.on_show = self.show
        GLib.timeout_add(400, self.remember_active)
        self.shown = True
        if hidden and self.tray:
            self.hide()

    # --- tray icon and showing / hiding -------------------------------------------------------

    def make_tray(self):
        if XApp is None:
            return None
        icon = XApp.StatusIcon()
        icon.set_name("ml-handwriting")
        self.tray_icon = ICON_IDLE
        icon.set_icon_name(ICON_IDLE)
        icon.set_tooltip_text("Malayalam Handwriting")
        menu = Gtk.Menu()
        self.mic_item = Gtk.MenuItem(label="Start listening")
        self.mic_item.connect("activate", lambda _i: self.toggle_mic())
        item_toggle = Gtk.MenuItem(label="Show / hide window")
        item_toggle.connect("activate", lambda _i: self.toggle())
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda _i: Gtk.main_quit())
        for item in (self.mic_item, item_toggle, Gtk.SeparatorMenuItem(), item_quit):
            menu.append(item)
        menu.show_all()
        icon.set_secondary_menu(menu)
        icon.connect("activate", self.on_tray_activate)
        icon.set_visible(True)
        return icon

    def set_recording(self, on):
        """Red tray icon while the microphone is listening."""
        if not self.tray:
            return
        if getattr(self, "pulse_id", None):
            GLib.source_remove(self.pulse_id)
            self.pulse_id = None
        if on:
            self.frame = 0
            self.pulse()
            self.pulse_id = GLib.timeout_add(PULSE_MS, self.pulse)   # glow: step through the red frames
        else:
            self.tray_icon = ICON_IDLE
            self.tray.set_icon_name(self.tray_icon)
        self.tray.set_tooltip_text("Malayalam Handwriting - listening" if on else "Malayalam Handwriting")
        self.mic_item.set_label("Stop listening" if on else "Start listening")

    def pulse(self):
        self.tray_icon = ICON_REC[self.frame % len(ICON_REC)]
        self.tray.set_icon_name(self.tray_icon)
        self.frame += 1
        return True

    def on_tray_activate(self, _icon, button, _time):
        if button == Gdk.BUTTON_PRIMARY:
            self.toggle()
        elif button == Gdk.BUTTON_MIDDLE:
            self.toggle_mic()

    def toggle_mic(self):
        """Start or stop listening from the tray, even while the window is hidden."""
        def done(view, res):
            try:
                answer = view.run_javascript_finish(res).get_js_value().to_string()
            except GLib.Error:
                return
            if answer == "nokey":
                self.show()                         # no key yet: show the window so Voice settings can be filled in
                self.view.run_javascript("document.getElementById('mic').click()", None, None, None)
        self.view.run_javascript(
            "(()=>{const m=document.getElementById('mic'); "
            "if (!m.classList.contains('on') && localStorage.getItem('ml-hw-engine') !== 'chrome' && !localStorage.getItem('ml-hw-key')) return 'nokey'; "
            "m.click(); return 'ok';})()", None, done)

    def remember_active(self):
        """Note which window you are working in, so it gets focus back after the window is shown."""
        try:
            wid = self.typer.active_window()
            if wid and wid != self.own_xid():
                cls = self.typer.disp.create_resource_object("window", wid).get_wm_class() or ("", "")
                if cls[1] not in ("Cinnamon", "Muffin", "Nemo-desktop", "Xdg-desktop-portal-gtk"):
                    self.last_real = wid
        except Exception:
            pass
        return True

    def own_xid(self):
        gdk_win = self.win.get_window()
        return gdk_win.get_xid() if gdk_win is not None and hasattr(gdk_win, "get_xid") else 0

    def toggle(self):
        if self.shown:
            self.hide()
        else:
            self.show()

    def show(self):
        if not self.shown:
            self.shown = True
            if self.ghosted:
                self.ghosted = False
                self.win.set_decorated(True)
                self.win.input_shape_combine_region(None)   # clickable again
                self.win.set_skip_taskbar_hint(False)
                self.win.set_opacity(0.999)   # not exactly 1.0: that removes the property and the window can stay see-through
            else:
                self.win.show()
        self.win.set_keep_above(True)
        self.view.run_javascript("window.__syncFocus && window.__syncFocus()", None, None, None)
        if not self.focus_wanted:
            GLib.timeout_add(150, lambda: (self.typer.activate(self.last_real), False)[1])   # keep typing where you were
        else:
            self.apply_focus()
        return False

    def hide(self):
        """Hide the window. The browser engine blocks the microphone for a really hidden window, and the tray
        can start listening, so the window stays mapped but is fully transparent, click-through and has no
        title bar or frame (the frame is what caught clicks). Its size is left alone: shrinking it broke the page."""
        self.shown = False
        self.apply_focus()          # a hidden window must not keep the keyboard
        screen = self.win.get_screen()
        if screen is not None and screen.is_composited() and self.win.get_window() is not None:
            self.ghosted = True
            self.win.set_decorated(False)
            self.win.set_skip_taskbar_hint(True)
            self.win.input_shape_combine_region(cairo.Region())   # clicks pass through
            self.win.set_opacity(0.0)
        else:
            self.ghosted = False        # no transparency on this desktop: really hide (the microphone then stops working)
            self.win.hide()

    def on_close(self, _win, _event):
        if self.tray:
            self.hide()
            return True    # keep running in the tray
        return False

    def on_message(self, _manager, result):
        value = result.get_js_value() if hasattr(result, "get_js_value") else result
        try:
            msg = json.loads(value.to_string())
        except (ValueError, TypeError):
            return
        if "log" in msg:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + str(msg["log"]) + "\n")
        elif "rec" in msg:
            self.set_recording(bool(msg["rec"]))
        elif "stt" in msg:
            self.speech_control(msg)
        elif "clip" in msg:
            self.typer.clip.set_text(str(msg["clip"]), -1)
            self.typer.clip.store()
        elif "focus" in msg:
            self.set_focus_mode(bool(msg["focus"]))
        else:
            self.deliver(msg)

    # --- Google speech through the hidden Chrome helper ------------------------------------------

    def call_page(self, function, arg):
        self.view.run_javascript(f"window.{function} && window.{function}({json.dumps(arg)})", None, None, None)

    def speech_control(self, msg):
        if msg["stt"] == "start":
            lang = msg.get("lang") if msg.get("lang") in ("ml-IN", "en-IN") else "ml-IN"
            problem = self.speech.start(lang)
            if problem:
                self.call_page("__sttStopped", problem)
                return
            self.speech_wanted = True
            self.speech_idle = 0
            GLib.timeout_add(1000, self.speech_watch)
        else:
            self.speech_wanted = False
            self.speech.stop()

    def speech_watch(self):
        """Notice if the helper died, and switch the microphone off after a long time with no speech."""
        if not self.speech_wanted:
            return False
        self.speech_idle += 1
        if not self.speech.alive():
            self.speech_wanted = False
            self.call_page("__sttStopped", "The speech helper stopped unexpectedly.")
            return False
        if self.speech_idle >= 180:
            self.speech_wanted = False
            self.speech.stop()
            self.call_page("__sttStopped", "Stopped listening after three minutes of silence.")
            return False
        return True

    def on_speech(self, report):
        if "final" in report:
            self.speech_idle = 0
            self.call_page("__sttFinal", str(report["final"]))
        elif "interim" in report:
            self.speech_idle = 0
            self.call_page("__sttInterim", str(report["interim"]))
        elif "error" in report:
            reasons = {
                "not-allowed": "Chrome was not allowed to use the microphone.",
                "service-not-allowed": "Google speech recognition is not allowed in this Chrome.",
                "network": "Google speech recognition could not be reached. Check the internet connection.",
                "audio-capture": "Chrome could not find a microphone.",
                "unsupported": "This Chrome has no speech recognition.",
            }
            self.speech_wanted = False
            self.speech.stop()
            self.call_page("__sttStopped", reasons.get(report["error"], "Speech error: " + str(report["error"])))
        return False

    def set_focus_mode(self, wanted):
        """The page asks for the keyboard (settings dialog, editing the text box, "This box only") or gives it up."""
        self.focus_wanted = wanted
        self.apply_focus()

    def apply_focus(self):
        want = self.focus_wanted and self.shown
        if want and not self.has_focus_mode:
            self.has_focus_mode = True
            self.previous = self.last_real or self.typer.active_window()
            self.win.set_accept_focus(True)
            self.win.present()
            self.typer.activate(self.own_xid())
            self.view.grab_focus()
        elif not want and self.has_focus_mode:
            self.has_focus_mode = False
            self.win.set_accept_focus(False)
            self.typer.activate(self.previous)
            self.previous = 0

    def deliver(self, msg):
        """Send words, Backspace or Enter to the other window. If this window holds the keyboard right now
        (you were editing the text box), give the keyboard back to that window first, then type there."""
        if self.releasing:
            self.held.append(msg)
        elif self.has_focus_mode:
            self.held.append(msg)
            self.releasing = True
            self.has_focus_mode = False
            self.win.set_accept_focus(False)
            self.typer.activate(self.previous or self.last_real)
            self.previous = 0
            GLib.timeout_add(250, self.flush_held)
        else:
            self.typer.push(msg)

    def flush_held(self):
        self.releasing = False
        held, self.held = self.held, []
        for msg in held:
            self.typer.push(msg)
        return False

    def on_permission(self, _view, request):
        if isinstance(request, WebKit2.UserMediaPermissionRequest):
            request.allow()
        else:
            request.deny()
        return True


def main():
    try:
        start_server()
    except OSError:
        # already running: ask that copy to show itself, then leave
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/__show", timeout=3).read()
            sys.exit(0)
        except OSError as err:
            sys.exit(f"Port {PORT} is in use by something else ({err}).")
    app = App(hidden="--hidden" in sys.argv)
    try:
        Gtk.main()
    finally:
        app.speech.stop()      # never leave a hidden Chrome running


if __name__ == "__main__":
    main()
