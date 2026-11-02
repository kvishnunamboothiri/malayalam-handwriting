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
import socketserver
import sys
import threading
import time
import urllib.request

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
SERVED = {"/", "/index.html", "/manifest.json", "/sw.js", "/icon-180.png", "/icon-512.png"}
DATA_DIR = os.path.expanduser("~/.local/share/ml-handwriting")
LOG_FILE = os.path.expanduser("~/.cache/ml-handwriting/debug.log")
CACHE_DIR = os.path.expanduser("~/.cache/ml-handwriting")


class Handler(http.server.SimpleHTTPRequestHandler):
    """Serves only the web app's own files, never the rest of the folder (such as .git)."""

    on_show = None   # set by the app: called when a second copy asks the first one to show itself

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
            data=(32, [1, X.CurrentTime, 0, 0, 0]),
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
        win.connect("delete-event", self.on_close)
        self.win = win
        self.previous = 0
        win.add(self.view)
        win.show_all()
        self.tray = self.make_tray()
        Handler.on_show = self.show
        GLib.timeout_add(400, self.remember_active)
        if hidden and self.tray:
            win.hide()

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
        item_toggle = Gtk.MenuItem(label="Show / hide")
        item_toggle.connect("activate", lambda _i: self.toggle())
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", lambda _i: Gtk.main_quit())
        for item in (item_toggle, Gtk.SeparatorMenuItem(), item_quit):
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

    def pulse(self):
        self.tray_icon = ICON_REC[self.frame % len(ICON_REC)]
        self.tray.set_icon_name(self.tray_icon)
        self.frame += 1
        return True

    def on_tray_activate(self, _icon, button, _time):
        if button == Gdk.BUTTON_PRIMARY:
            self.toggle()

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
        if self.win.get_visible():
            self.hide()
        else:
            self.show()

    def show(self):
        if not self.win.get_visible():
            self.win.show()
        self.win.set_keep_above(True)
        GLib.timeout_add(150, lambda: (self.typer.activate(self.last_real), False)[1])   # keep typing where you were
        return False

    def hide(self):
        # stop listening first so the microphone is not left on in a hidden window
        self.view.run_javascript(
            "(()=>{const m=document.getElementById('mic'); if (m && m.classList.contains('on')) m.click();})()",
            None, None, None)
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
    except OSError:
        # already running: ask that copy to show itself, then leave
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/__show", timeout=3).read()
            sys.exit(0)
        except OSError as err:
            sys.exit(f"Port {PORT} is in use by something else ({err}).")
    App(hidden="--hidden" in sys.argv)
    Gtk.main()


if __name__ == "__main__":
    main()
