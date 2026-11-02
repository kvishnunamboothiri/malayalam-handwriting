#!/usr/bin/env python3
"""Malayalam handwriting / voice pad for Linux (X11).

A small always-on-top window that never takes keyboard focus. Words you write or
speak are pasted into whichever window you were typing in (for example your browser).
It runs the same web app as the phone version, served from this folder's parent.
"""
import functools
import http.server
import json
import os
import socketserver
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk, WebKit2  # noqa: E402
from Xlib import X, XK, display, protocol  # noqa: E402
from Xlib.ext import xtest  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 18765
SERVED = {"/", "/index.html", "/manifest.json", "/sw.js", "/icon-180.png", "/icon-512.png"}
DATA_DIR = os.path.expanduser("~/.local/share/ml-handwriting")
LOG_FILE = os.path.expanduser("~/.cache/ml-handwriting/debug.log")
CACHE_DIR = os.path.expanduser("~/.cache/ml-handwriting")


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves only the web app's own files, never the rest of the folder (such as .git)."""

    def do_GET(self):
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
            data=(32, [1, X.CurrentTime, 0, 0, 0]),
        )
        self.disp.screen().root.send_event(ev, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
        self.disp.flush()

    def push(self, msg):
        self.queue.append(msg)
        self._pump()

    def _pump(self):
        if self.busy or not self.queue:
            return
        self.busy = True
        msg = self.queue.pop(0)
        if "text" in msg:
            self.clip.set_text(msg["text"], -1)
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


class App:
    def __init__(self):
        self.typer = Typer()
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
        self.view.connect("permission-request", self.on_permission)
        self.view.load_uri(f"http://localhost:{PORT}/")

        win = Gtk.Window(title="Malayalam Handwriting")
        win.set_default_size(430, 500)   # compact: dictation is the main use here, the pad is secondary
        win.set_accept_focus(False)        # clicks here must not steal focus from the window you type in
        win.set_focus_on_map(False)
        win.set_keep_above(True)
        win.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        try:
            win.set_icon_from_file(os.path.join(ROOT, "icon-512.png"))
        except GLib.Error:
            pass
        win.connect("destroy", Gtk.main_quit)
        self.win = win
        self.previous = 0
        win.add(self.view)
        win.show_all()

    def on_message(self, _manager, result):
        value = result.get_js_value() if hasattr(result, "get_js_value") else result
        try:
            msg = json.loads(value.to_string())
        except (ValueError, TypeError):
            return
        if "log" in msg:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(time.strftime("%H:%M:%S ") + str(msg["log"]) + "\n")
        elif "clip" in msg:
            self.typer.clip.set_text(str(msg["clip"]), -1)
            self.typer.clip.store()
        elif "focus" in msg:
            self.set_focus_mode(bool(msg["focus"]))
        else:
            self.typer.push(msg)

    def set_focus_mode(self, wanted):
        """Take keyboard focus while a text field here needs it (the settings dialog), then give it back."""
        if wanted:
            self.previous = self.typer.active_window()
            self.win.set_accept_focus(True)
            self.win.present()
            self.view.grab_focus()
        else:
            self.win.set_accept_focus(False)
            self.typer.activate(self.previous)
            self.previous = 0

    def on_permission(self, _view, request):
        if isinstance(request, WebKit2.UserMediaPermissionRequest):
            request.allow()
        else:
            request.deny()
        return True


def main():
    try:
        start_server()
    except OSError as err:
        sys.exit(f"Port {PORT} is busy ({err}). Close the other copy of this app.")
    App()
    Gtk.main()


if __name__ == "__main__":
    main()
