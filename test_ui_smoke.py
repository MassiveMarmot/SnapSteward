# SPDX-License-Identifier: GPL-3.0-or-later
import copy
import os
import re
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_snapd_client import MockSnapd  # noqa: E402

import snapd_client  # noqa: E402
import changes  # noqa: E402
import gi  # noqa: E402
gi.require_version("Adw", "1")  # noqa: E402

from gi.repository import Adw, GLib, Graphene, Gtk, Pango  # noqa: E402

SNAP_APP = {"name": "firefox", "type": "app", "version": "1.0",
            "summary": "Browse the web", "apps": [{"name": "firefox"}]}
SNAP_BASE = {"name": "core24", "type": "base", "version": "2"}

_SHARED = {}


def shared_app():
    """One Adw.Application per process: registering the same app id
    twice fails, so all test classes share it."""
    if "app" not in _SHARED:
        import main
        _SHARED["main"] = main
        app = main.App()
        app.connect("activate", lambda a: None)
        app.register()
        _SHARED["app"] = app
    return _SHARED["main"], _SHARED["app"]

CONNECTIONS = {
    "established": [
        {"slot": {"snap": "snapd", "slot": "camera"},
         "plug": {"snap": "firefox", "plug": "camera"},
         "interface": "camera", "manual": True},
        {"slot": {"snap": "snapd", "slot": "network"},
         "plug": {"snap": "firefox", "plug": "network"},
         "interface": "network"},
        {"slot": {"snap": "snapd", "slot": "removable-media"},
         "plug": {"snap": "firefox", "plug": "removable-media"},
         "interface": "removable-media", "manual": True},
    ],
    "undesired": [
        {"slot": {"snap": "snapd", "slot": "audio-record"},
         "plug": {"snap": "firefox", "plug": "audio-record"},
         "interface": "audio-record", "manual": True},
    ],
    "plugs": [
        {"snap": "firefox", "plug": "camera", "interface": "camera",
         "apps": [], "connections": [{"snap": "snapd", "slot": "camera"}]},
        {"snap": "firefox", "plug": "network", "interface": "network",
         "apps": [], "connections": []},
        {"snap": "firefox", "plug": "removable-media",
         "interface": "removable-media", "apps": [], "connections": []},
        {"snap": "firefox", "plug": "audio-record",
         "interface": "audio-record", "apps": [], "connections": []},
        {"snap": "firefox", "plug": "docker-support",
         "interface": "docker-support", "apps": [], "connections": []},
        {"snap": "firefox", "plug": "orphan", "interface": "orphan-if",
         "apps": [], "connections": []},
        {"snap": "firefox", "plug": "gtk-3-themes", "interface": "content",
         "apps": [], "connections": []},
    ],
    "slots": [
        {"snap": "snapd", "slot": "camera", "interface": "camera",
         "connections": []},
        {"snap": "snapd", "slot": "network", "interface": "network",
         "connections": []},
        {"snap": "snapd", "slot": "removable-media",
         "interface": "removable-media", "connections": []},
        {"snap": "snapd", "slot": "audio-record",
         "interface": "audio-record", "connections": []},
    ],
}


class UISmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.socket_path = os.path.join(cls.tmpdir.name, "snapd.socket")
        cls.server = MockSnapd(cls.socket_path)
        os.environ["SNAPD_SOCKET"] = cls.socket_path
        cls.app = None
        cls.main = None

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.main, self.app = shared_app()
        self.server.snaps = []
        self.server.posts = []
        self.server.responses = []
        self.server.interface_responses = []
        self.server.change_script = {}
        self.server.next_change_id = 1
        self.server.delay = 0
        self.server.default_connections = {
            "established": [], "undesired": [], "plugs": [], "slots": []}
        self.prev_data_dir = os.environ.get("GINGER_DATA_DIR")
        self.tmpdir_i = tempfile.TemporaryDirectory()
        os.environ["GINGER_DATA_DIR"] = self.tmpdir_i.name
        self.win = self.main.Window(self.app)
        self.win.present()

    def tearDown(self):
        self.win.destroy()
        self.tmpdir_i.cleanup()
        if self.prev_data_dir is None:
            del os.environ["GINGER_DATA_DIR"]
        else:
            os.environ["GINGER_DATA_DIR"] = self.prev_data_dir

    def make_window(self):
        self.win.load()
        return self.win

    def find_labels(self, widget, out):
        if isinstance(widget, Gtk.Label):
            out.append(widget)
        child = widget.get_first_child()
        while child:
            self.find_labels(child, out)
            child = child.get_next_sibling()
        return out

    def row_texts(self, win):
        texts = []
        for i in range(200):
            row = win.snaps_list.get_row_at_index(i)
            if row is None:
                break
            texts.append([l.get_text() for l in self.find_labels(row, [])])
        return texts

    def walk(self, widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from self.walk(child)
            child = child.get_next_sibling()

    def wait_change_finished(self, win):
        # start_change sets busy synchronously when the dialog response is
        # emitted, so waiting for True then False is race-free.
        self.run_until(lambda: win.busy is True)
        self.run_until(lambda: win.busy is False)

    def run_until(self, condition, timeout_ms=2000):
        ctx = GLib.MainContext.default()
        end = GLib.get_monotonic_time() + timeout_ms * 1000
        while not condition() and GLib.get_monotonic_time() < end:
            if not ctx.iteration(False):
                time.sleep(0.01)
        self.assertTrue(condition(), "run_until timed out")

    def test_funnel_icon_loads(self):
        self.make_window()
        theme = Gtk.IconTheme.get_for_display(self.win.get_display())
        self.assertTrue(theme.has_icon("funnel-symbolic"))
        self.assertEqual(self.win.filter_button.get_icon_name(),
                         "funnel-symbolic")

    def test_summary_ellipsizes_at_end(self):
        self.server.snaps = [dict(SNAP_APP)]
        win = self.make_window()
        win.on_snap_selected(win.snaps_list, win.snaps_list.get_row_at_index(0))
        summary = [l for l in self.find_labels(win.detail_bin.get_child(), [])
                   if l.get_text() == "Browse the web"][0]
        self.assertEqual(summary.get_ellipsize(), Pango.EllipsizeMode.END)

    def test_auto_select_does_not_reveal_content(self):
        self.server.snaps = [dict(SNAP_APP)]
        win = self.make_window()
        win.detail_split.set_collapsed(True)
        win.detail_split.set_show_content(False)
        win.load()
        self.assertEqual(win.selected_snap, "firefox")
        self.assertFalse(win.detail_split.get_show_content())
        win.on_snap_selected(win.snaps_list,
                             win.snaps_list.get_row_at_index(0))
        self.assertTrue(win.detail_split.get_show_content())

    def test_info_rows_use_property_look(self):
        self.server.snaps = [dict(SNAP_APP)]
        win = self.make_window()
        win.on_snap_selected(win.snaps_list, win.snaps_list.get_row_at_index(0))
        rows = [r for r in self.walk(win.detail_bin.get_child())
                if isinstance(r, Adw.ActionRow)]
        titles = [r.get_title() for r in rows]
        for title in ("Snap name", "Version"):
            row = rows[titles.index(title)]
            self.assertIn("property", row.get_css_classes(), title)
            self.assertTrue(row.get_subtitle(), title)

    def test_apps_empty_snap_hidden(self):
        lib = {"name": "gtk-common-themes", "type": "app", "apps": []}
        self.server.snaps = [dict(SNAP_APP), lib]
        win = self.make_window()
        names = [t[0] for t in self.row_texts(win)]
        self.assertNotIn("gtk-common-themes", names)
        win.lib_check.set_active(True)
        names = [t[0] for t in self.row_texts(win)]
        self.assertIn("gtk-common-themes", names)

    def test_no_apps_field_behaves_like_empty(self):
        lib = {"name": "gtk-common-themes", "type": "app"}
        self.server.snaps = [dict(SNAP_APP), lib]
        win = self.make_window()
        names = [t[0] for t in self.row_texts(win)]
        self.assertNotIn("gtk-common-themes", names)

    def test_markup_in_version_not_parsed(self):
        snap = dict(SNAP_APP, version="<b>evil</b>&amp;")
        self.server.snaps = [snap]
        win = self.make_window()
        win.on_snap_selected(win.snaps_list, win.snaps_list.get_row_at_index(0))
        texts = [l.get_text() for l in
                 self.find_labels(win.detail_bin.get_child(), [])]
        self.assertIn("<b>evil</b>&amp;", texts)

    def test_sidebar_single_entry(self):
        win = self.make_window()
        row = win.sidebar_rows.get_row_at_index(0)
        self.assertEqual(row.page_name, "Snaps")

    def test_list_includes_snaps(self):
        self.server.snaps = [dict(SNAP_APP), dict(SNAP_BASE),
                             {"name": "nicotine-plus", "type": "app",
                              "apps": [{"name": "nicotine-plus"}]}]
        win = self.make_window()
        names = [t[0] for t in self.row_texts(win)]
        self.assertIn("firefox", names)
        self.assertIn("nicotine-plus", names)
        self.assertNotIn("core24", names)

    def test_search_by_name(self):
        self.server.snaps = [dict(SNAP_APP),
                             {"name": "thunderbird", "type": "app",
                              "apps": [{"name": "thunderbird"}]}]
        win = self.make_window()
        win.query = "thunder"
        win.refresh_list()
        texts = self.row_texts(win)
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0][0], "thunderbird")

    def test_selecting_snap_closes_filter(self):
        self.server.snaps = [dict(SNAP_APP)]
        win = self.make_window()
        win.filter_button.set_active(True)
        self.assertEqual(win.detail_page.get_child(), win.filter_panel)
        win.on_snap_selected(win.snaps_list,
                             win.snaps_list.get_row_at_index(0))
        self.assertFalse(win.filter_button.get_active())
        self.assertEqual(win.detail_page.get_child(), win.detail_pane)

    def test_funnel_reveals_content_when_collapsed(self):
        self.server.snaps = [dict(SNAP_APP)]
        win = self.make_window()
        win.detail_split.set_collapsed(True)
        win.detail_split.set_show_content(False)
        win.filter_button.set_active(True)
        self.assertTrue(win.detail_split.get_show_content())
        self.assertEqual(win.detail_page.get_child(), win.filter_panel)

    def test_detail_pane_contents(self):
        self.server.snaps = [dict(SNAP_APP)]
        win = self.make_window()
        win.on_snap_selected(win.snaps_list,
                             win.snaps_list.get_row_at_index(0))
        texts = [l.get_text() for l in
                 self.find_labels(win.detail_bin.get_child(), [])]
        self.assertIn("firefox", texts)
        self.assertIn("Browse the web", texts)
        self.assertIn("1.0", texts)

    def test_selection_and_query_survive_refresh(self):
        self.server.snaps = [dict(SNAP_APP),
                             {"name": "thunderbird", "type": "app",
                              "apps": [{"name": "thunderbird"}]}]
        win = self.make_window()
        win.on_snap_selected(win.snaps_list,
                             win.snaps_list.get_row_at_index(1))
        self.assertEqual(win.selected_snap, "thunderbird")
        win.query = "e"
        win.refresh_list()
        win.activate_action("win.refresh", None)
        self.assertEqual(win.selected_snap, "thunderbird")
        row = win.snaps_list.get_selected_row()
        self.assertEqual(row.snap_name, "thunderbird")

    def test_show_content_on_selection(self):
        self.server.snaps = [dict(SNAP_APP)]
        win = self.make_window()
        win.detail_split.set_collapsed(True)
        win.detail_split.set_show_content(False)
        win.on_snap_selected(win.snaps_list,
                             win.snaps_list.get_row_at_index(0))
        self.assertTrue(win.detail_split.get_show_content())

    def test_error_then_recovery(self):
        self.server.responses.append((400, {
            "type": "error", "status-code": 400,
            "result": {"message": "<b>evil</b>&amp;", "kind": "some-kind"},
        }))
        win = self.make_window()
        self.assertEqual(win.page_stack.get_visible_child_name(), "error")
        self.assertEqual(win.error_page.get_title(), "snapd returned an error")
        texts = [l.get_text() for l in self.find_labels(win.error_page, [])]
        self.assertIn("<b>evil</b>&amp;", texts)
        self.server.snaps = [dict(SNAP_APP)]
        win.activate_action("win.refresh", None)
        self.assertEqual(win.page_stack.get_visible_child_name(), "snaps")
        self.assertEqual(len(self.row_texts(win)), 1)

    def test_connection_error_page(self):
        sock_path = os.path.join(self.tmpdir.name, "missing.socket")
        client = snapd_client.Client(socket_path=sock_path)
        with self.assertRaises(snapd_client.SnapdError) as ctx:
            client.list_snaps()
        self.assertEqual(ctx.exception.kind, "connection-failed")
        win = self.make_window()
        win.show_error("Could not reach snapd", ctx.exception.message,
                       "network-error-symbolic")
        self.assertEqual(win.error_page.get_title(), "Could not reach snapd")

    def test_markup_in_snap_name_not_parsed(self):
        self.server.snaps = [{"name": "<b>evil</b>&amp;", "type": "app",
                              "apps": [{"name": "x"}]}]
        win = self.make_window()
        texts = self.row_texts(win)
        self.assertEqual(texts[0][0], "<b>evil</b>&amp;")

    def test_snap_rows_are_activatable(self):
        self.server.snaps = [dict(SNAP_APP)]
        win = self.make_window()
        self.assertTrue(win.snaps_list.get_row_at_index(0).get_activatable())

    def test_minimal_snap_object(self):
        self.server.snaps = [{"name": "x"}]
        win = self.make_window()
        self.assertEqual(self.row_texts(win), [])
        self.assertIsNone(win.selected_snap)

    def test_breakpoint_700px(self):
        self.server.snaps = [dict(SNAP_APP)]
        win = self.make_window()
        win.set_default_size(700, 600)
        self.run_until(lambda: win.main_split.get_collapsed())
        self.assertTrue(win.main_split.get_collapsed())
        self.assertFalse(win.detail_split.get_collapsed())

    def test_breakpoint_500px(self):
        self.server.snaps = [dict(SNAP_APP)]
        win = self.make_window()
        win.set_default_size(500, 600)
        self.run_until(lambda: win.main_split.get_collapsed()
                       and win.detail_split.get_collapsed())
        self.assertTrue(win.main_split.get_collapsed())
        self.assertTrue(win.detail_split.get_collapsed())

    def test_collapsed_sidebar_toggle_reopens(self):
        win = self.make_window()
        win.main_split.set_collapsed(True)
        win.main_split.set_show_sidebar(False)
        self.assertFalse(win.main_split.get_show_sidebar())
        self.assertFalse(win.sidebar_toggle.get_active())
        win.sidebar_toggle.set_active(True)
        self.assertTrue(win.main_split.get_show_sidebar())
        win.main_split.set_show_sidebar(False)
        self.assertFalse(win.sidebar_toggle.get_active())

    def test_window_title(self):
        win = self.make_window()
        self.assertEqual(win.get_title(), "Ginger")

    def permission_rows(self, win):
        return [r for r in self.walk(win.detail_bin.get_child())
                if isinstance(r, Adw.SwitchRow)]

    def row_by_title(self, win, title):
        for row in self.permission_rows(win):
            if row.get_title() == title:
                return row
        return None

    def test_snap_counts_in_list(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        win = self.make_window()
        row = win.snaps_list.get_row_at_index(0)
        texts = [l.get_text() for l in self.find_labels(row, [])]
        self.assertTrue(any("3 connected" in t for t in texts), texts)
        self.assertTrue(any("1 available" in t for t in texts), texts)

    def test_permissions_group_states(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        win = self.make_window()
        win.on_snap_selected(win.snaps_list, win.snaps_list.get_row_at_index(0))
        camera = self.row_by_title(win, "camera")
        self.assertIsNotNone(camera)
        self.assertTrue(camera.get_active())
        self.assertIn("Manually connected", camera.get_subtitle())
        network = self.row_by_title(win, "network")
        self.assertTrue(network.get_active())
        self.assertIn("Connected", network.get_subtitle())
        self.assertNotIn("Manually", network.get_subtitle())
        audio = self.row_by_title(win, "audio-record")
        self.assertFalse(audio.get_active())
        self.assertIn("Manually disconnected", audio.get_subtitle())

    def test_no_slot_available(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        win = self.make_window()
        win.on_snap_selected(win.snaps_list, win.snaps_list.get_row_at_index(0))
        orphan = self.row_by_title(win, "orphan")
        self.assertIsNotNone(orphan)
        self.assertIn("No slot available", orphan.get_subtitle())
        self.assertFalse(orphan.get_sensitive())

    def test_content_plug_hidden_by_default(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        win = self.make_window()
        win.on_snap_selected(win.snaps_list, win.snaps_list.get_row_at_index(0))
        self.assertIsNone(self.row_by_title(win, "gtk-3-themes"))
        win.all_check.set_active(True)
        self.assertIsNotNone(self.row_by_title(win, "gtk-3-themes"))

    def test_tier_sort_order(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        win = self.make_window()
        win.on_snap_selected(win.snaps_list, win.snaps_list.get_row_at_index(0))
        titles = [r.get_title() for r in self.permission_rows(win)]
        self.assertEqual(titles, ["audio-record", "camera", "network",
                                 "orphan", "removable-media",
                                 "docker-support"])

    def test_tier_3_connect_not_offered(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        win = self.make_window()
        win.on_snap_selected(win.snaps_list, win.snaps_list.get_row_at_index(0))
        docker = self.row_by_title(win, "docker-support")
        self.assertIn("Connect not offered", docker.get_subtitle())

    def test_markup_in_plug_name_not_parsed(self):
        snap = dict(SNAP_APP, name="<b>evil</b>&amp;")
        conns = dict(CONNECTIONS)
        conns["plugs"] = [dict(p) for p in CONNECTIONS["plugs"]]
        conns["plugs"][0] = {"snap": "<b>evil</b>&amp;", "plug": "camera",
                             "interface": "camera", "apps": [],
                             "connections": []}
        conns["established"] = [
            {"slot": {"snap": "snapd", "slot": "camera"},
             "plug": {"snap": "<b>evil</b>&amp;", "plug": "camera"},
             "interface": "camera", "manual": True}]
        self.server.snaps = [snap]
        self.server.default_connections = conns
        win = self.make_window()
        win.on_snap_selected(win.snaps_list, win.snaps_list.get_row_at_index(0))
        texts = [l.get_text() for l in
                 self.find_labels(win.detail_bin.get_child(), [])]
        self.assertIn("<b>evil</b>&amp;", texts)

    def test_baseline_written_at_launch(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        self.make_window()
        path = os.path.join(os.environ["GINGER_DATA_DIR"],
                            "baseline.json")
        self.assertTrue(os.path.exists(path))
        import baseline
        loaded = baseline.load(path)
        self.assertIn("firefox", loaded)
        self.assertEqual(
            sorted(c["plug"] for c in loaded["firefox"]["connected"]),
            ["camera", "network", "removable-media"])

    def test_baseline_not_overwritten_across_loads(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        self.make_window()
        path = os.path.join(os.environ["GINGER_DATA_DIR"],
                            "baseline.json")
        with open(path) as f:
            first = f.read()
        self.server.default_connections = {
            "established": [], "undesired": [], "plugs": [], "slots": []}
        self.win.load()
        with open(path) as f:
            self.assertEqual(f.read(), first)

    def test_corrupt_baseline_shows_banner_not_blocked(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        os.makedirs(os.environ["GINGER_DATA_DIR"], exist_ok=True)
        for name in ("baseline.json", "baseline.json.bak"):
            with open(os.path.join(os.environ["GINGER_DATA_DIR"],
                                   name), "w") as f:
                f.write("not-json{")
        win = self.make_window()
        self.assertTrue(win.baseline_banner.get_revealed())
        self.assertEqual(win.page_stack.get_visible_child_name(), "snaps")
        self.assertEqual(len(self.row_texts(win)), 1)
        path = os.path.join(os.environ["GINGER_DATA_DIR"],
                            "baseline.json")
        with open(path) as f:
            self.assertEqual(f.read(), "not-json{")

    @unittest.skipIf(os.geteuid() == 0, "chmod is ineffective as root")
    def test_baseline_save_failure_shows_banner(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        data_dir = os.environ["GINGER_DATA_DIR"]
        os.chmod(data_dir, 0o500)
        try:
            win = self.make_window()
            self.assertTrue(win.baseline_banner.get_revealed())
            self.assertEqual(win.page_stack.get_visible_child_name(),
                             "snaps")
            self.assertEqual(len(self.row_texts(win)), 1)
        finally:
            os.chmod(data_dir, 0o700)

    def test_new_saved_state_keeps_unreadable_files(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        os.makedirs(os.environ["GINGER_DATA_DIR"], exist_ok=True)
        for name in ("baseline.json", "baseline.json.bak"):
            with open(os.path.join(os.environ["GINGER_DATA_DIR"],
                                   name), "w") as f:
                f.write("not-json{")
        with open(os.path.join(os.environ["GINGER_DATA_DIR"],
                               "baseline.json.bak"), "w") as f:
            f.write("also not json")
        win = self.make_window()
        self.assertTrue(win.baseline_banner.get_revealed())
        self.assertIn("cannot be read", win.baseline_banner.get_title())
        self.assertIn(os.environ["GINGER_DATA_DIR"],
                      win.baseline_banner.get_title())
        self.assert_switches_live(win, False)
        win.baseline_banner.emit("button-clicked")
        alert = self.wait_alert("Start a new saved state")
        self.assertIn("no longer be the state from before Ginger",
                      alert.get_body())
        alert.emit("response", "restart")
        self.run_until(lambda: not win.baseline_banner.get_revealed())
        files = sorted(os.listdir(os.environ["GINGER_DATA_DIR"]))
        self.assertEqual(len(files), 3, files)
        self.assertTrue(all(
            "baseline.unreadable-" in f for f in files
            if not f.startswith("baseline.json")), files)
        import baseline
        loaded, used_backup = baseline.load()
        self.assertFalse(used_backup)
        self.assertIn("firefox", loaded)
        self.assert_switches_live(win, True)

    def test_cancel_new_saved_state_keeps_files_blocked(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        os.makedirs(os.environ["GINGER_DATA_DIR"], exist_ok=True)
        for name in ("baseline.json", "baseline.json.bak"):
            with open(os.path.join(os.environ["GINGER_DATA_DIR"],
                                   name), "w") as f:
                f.write("not-json{")
        win = self.make_window()
        self.assertTrue(win.baseline_banner.get_revealed())
        win.baseline_banner.emit("button-clicked")
        alert = self.wait_alert("Start a new saved state")
        alert.emit("response", "cancel")
        self.run_until(lambda: self.win.baseline_banner.get_revealed())
        with open(os.path.join(os.environ["GINGER_DATA_DIR"],
                               "baseline.json")) as f:
            self.assertEqual(f.read(), "not-json{")
        self.assert_switches_live(self.win, False)

    def test_backup_used_when_main_corrupt(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        win = self.make_window()
        path = os.path.join(os.environ["GINGER_DATA_DIR"], "baseline.json")
        with open(path) as f:
            good = f.read()
        self.assertFalse(win.baseline_banner.get_revealed())
        with open(path, "w") as f:
            f.write("not-json{")
        win.load()
        self.assertFalse(win.baseline_banner.get_revealed())
        self.assertIn("backup copy of the saved original state",
                      win.last_toast.get_title())
        self.assert_switches_live(win, True)

    def test_banner_hidden_when_baseline_ok(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        win = self.make_window()
        self.assertFalse(win.baseline_banner.get_revealed())

    def switch_rows(self, win):
        return [r for r in self.walk(win.detail_bin.get_child())
                if isinstance(r, Adw.SwitchRow)]

    def switch_row(self, win, plug):
        for r in self.switch_rows(win):
            if r.plug_name == plug:
                return r
        return None

    def load_win(self, snaps=None, connections=None):
        self.server.snaps = snaps or [dict(SNAP_APP)]
        if connections is not None:
            self.server.default_connections = connections
        else:
            self.server.default_connections = CONNECTIONS
        win = self.make_window()
        win.on_snap_selected(win.snaps_list, win.snaps_list.get_row_at_index(0))
        return win

    def assert_switches_live(self, win, expected_sensitive):
        for row in self.switch_rows(win):
            if row.plug_info["tier"] == 3 and not row.plug_info["connected"]:
                continue
            if row.plug_slot is None:
                continue
            self.assertEqual(row.get_sensitive(), expected_sensitive)

    def test_switches_live_when_baseline_exists(self):
        win = self.load_win()
        self.assert_switches_live(win, True)

    def test_switches_dead_when_baseline_problem(self):
        # A corrupt baseline keeps the gate closed: no change may be sent
        # while the saved original state is unreadable.
        os.makedirs(os.environ["GINGER_DATA_DIR"], exist_ok=True)
        for name in ("baseline.json", "baseline.json.bak"):
            with open(os.path.join(os.environ["GINGER_DATA_DIR"],
                                   name), "w") as f:
                f.write("not-json{")
        win = self.load_win()
        self.assertTrue(win.baseline_banner.get_revealed())
        self.assert_switches_live(win, False)

    @unittest.skipIf(os.geteuid() == 0, "chmod is ineffective as root")
    def test_switches_dead_when_baseline_save_fails(self):
        # New snaps get a baseline at launch; if it cannot be saved,
        # the snap has no baseline and the gate stays closed.
        data_dir = os.environ["GINGER_DATA_DIR"]
        os.chmod(data_dir, 0o500)
        try:
            win = self.load_win()
            self.assertTrue(win.baseline_banner.get_revealed())
            self.assert_switches_live(win, False)
        finally:
            os.chmod(data_dir, 0o700)

    def toggle_switch(self, row):
        # Click the real switch child, like a user does.
        for child in self.walk(row):
            if isinstance(child, Gtk.Switch):
                child.emit("activate")
                return
        raise AssertionError("no switch inside row")

    def confirm_alert(self, heading_part, response, timeout_ms=3000):
        alert = self.wait_alert(heading_part, timeout_ms)
        if alert is not None:
            alert.emit("response", response)
        return alert

    def wait_alert(self, heading_part, timeout_ms=3000):
        ctx = GLib.MainContext.default()
        end = GLib.get_monotonic_time() + timeout_ms * 1000
        alert = []
        while not alert and GLib.get_monotonic_time() < end:
            alert = [d for w in Gtk.Window.list_toplevels()
                     for d in self.walk(w)
                     if isinstance(d, Adw.AlertDialog)
                     and heading_part in d.get_heading()]
            self.assertEqual(len(alert), 1,
                            "expected exactly one %s alert" % heading_part)
            if not alert and not ctx.iteration(False):
                time.sleep(0.01)
        self.assertTrue(alert, "no %s alert appeared" % heading_part)
        return alert[0] if alert else None

    def test_header_sent_and_202_to_done(self):
        win = self.load_win()
        row = self.switch_row(win, "camera")
        self.assertTrue(row.get_active())
        self.toggle_switch(row)
        self.assertIsNotNone(self.confirm_alert("Disconnect", "confirm"))
        self.run_until(lambda: self.server.posts)
        path, body, allowed = self.server.posts[0]
        self.assertEqual(path, "/v2/interfaces")
        self.assertTrue(allowed)
        self.assertEqual(body, {
            "action": "disconnect",
            "plugs": [{"snap": "firefox", "plug": "camera"}],
            "slots": [{"snap": "snapd", "slot": "camera"}]})
        self.wait_change_finished(win)

    def test_change_error_shows_plain_text_and_reverts(self):
        win = self.load_win()
        row = self.switch_row(win, "camera")
        self.server.change_script["1"] = [
            {"status": "Error", "ready": True, "err": "<b>nope</b>&amp;",
             "summary": "failed"}]
        self.toggle_switch(row)
        self.confirm_alert("Disconnect", "confirm")
        self.wait_change_finished(win)
        error = [d for w in Gtk.Window.list_toplevels()
                 for d in self.walk(w) if isinstance(d, Adw.AlertDialog)
                 and d.get_heading() == "snapd returned an error"]
        self.assertTrue(error)
        self.assertEqual(error[0].get_body(), "<b>nope</b>&amp;")
        row = self.switch_row(win, "camera")
        self.assertTrue(row.get_active())

    def test_auth_cancelled_silent_revert(self):
        win = self.load_win()
        row = self.switch_row(win, "camera")
        self.server.interface_responses.append((403, {
            "type": "error", "status-code": 403,
            "result": {"message": "cancelled",
                       "kind": "auth-cancelled"}}))
        self.toggle_switch(row)
        self.confirm_alert("Disconnect", "confirm")
        self.wait_change_finished(win)
        self.run_until(lambda: self.switch_row(win, "camera") is not None)
        row = self.switch_row(win, "camera")
        self.assertTrue(row.get_active())

    def test_tier1_connect_no_confirmation(self):
        conns = dict(CONNECTIONS)
        conns["established"] = [e for e in CONNECTIONS["established"]
                                 if e["plug"]["plug"] != "camera"]
        conns["undesired"] = list(CONNECTIONS["undesired"]) + [
            {"slot": {"snap": "snapd", "slot": "camera"},
             "plug": {"snap": "firefox", "plug": "camera"},
             "interface": "camera", "manual": True}]
        conns["plugs"] = [dict(p) for p in CONNECTIONS["plugs"]]
        win = self.load_win(connections=conns)
        row = self.switch_row(win, "camera")
        self.assertFalse(row.get_active())
        self.toggle_switch(row)
        self.run_until(lambda: self.server.posts)
        path, body, allowed = self.server.posts[0]
        self.assertEqual(body["action"], "connect")

    def test_tier2_connect_shows_confirmation(self):
        conns = dict(CONNECTIONS)
        conns["established"] = [e for e in CONNECTIONS["established"]
                                 if e["plug"]["plug"] != "removable-media"]
        conns["undesired"] = list(CONNECTIONS["undesired"]) + [
            {"slot": {"snap": "snapd", "slot": "removable-media"},
             "plug": {"snap": "firefox", "plug": "removable-media"},
             "interface": "removable-media", "manual": True}]
        conns["plugs"] = [dict(p) for p in CONNECTIONS["plugs"]]
        win = self.load_win(connections=conns)
        row = self.switch_row(win, "removable-media")
        self.assertFalse(row.get_active())
        self.toggle_switch(row)
        alert = self.wait_alert("Connect")
        self.assertIn("/media", alert.get_body())
        alert.emit("response", "confirm")
        self.run_until(lambda: self.server.posts)
        path, body, allowed = self.server.posts[0]
        self.assertEqual(body["action"], "connect")
        self.wait_change_finished(win)
        self.run_until(lambda: self.switch_row(win, "removable-media")
                       is not None)
        row = self.switch_row(win, "removable-media")
        self.assertFalse(row.get_active())

    def test_disconnect_confirmation_cancel_reverts(self):
        win = self.load_win()
        row = self.switch_row(win, "camera")
        self.toggle_switch(row)
        alert = self.wait_alert("Disconnect")
        self.assertEqual(alert.get_body(),
                         changes.confirmation_body(
                             "disconnect", "camera", "camera", 1))
        self.assertEqual(self.server.posts, [])
        alert.emit("response", "cancel")
        self.run_until(lambda: self.switch_row(win, "camera") is not None)
        row = self.switch_row(win, "camera")
        self.assertTrue(row.get_active())

    def test_tier3_connect_not_offered(self):
        conns = dict(CONNECTIONS)
        conns["slots"] = list(CONNECTIONS["slots"]) + [
            {"snap": "snapd", "slot": "docker-support",
             "interface": "docker-support", "connections": []}]
        win = self.load_win(connections=conns)
        row = self.switch_row(win, "docker-support")
        self.assertFalse(row.get_active())
        self.assertIn("Connect not offered", row.get_subtitle())
        self.assertFalse(row.get_sensitive())

    def test_busy_flag_blocks_second_change(self):
        win = self.load_win()
        row = self.switch_row(win, "camera")
        self.server.delay = 0.5
        self.toggle_switch(row)
        self.confirm_alert("Disconnect", "confirm")
        self.run_until(lambda: win.busy is True)
        row2 = self.switch_row(win, "network")
        self.toggle_switch(row2)
        self.assertTrue(row2.get_active())
        self.run_until(lambda: win.busy is False)
        self.assertEqual(len(self.server.posts), 1)
        self.server.delay = 0

    def test_undo_toast_sends_inverse(self):
        win = self.load_win()
        row = self.switch_row(win, "camera")
        self.toggle_switch(row)
        self.confirm_alert("Disconnect", "confirm")
        self.wait_change_finished(win)
        toast = win.last_toast
        self.assertIsNotNone(toast)
        self.assertIn("Disconnected camera", toast.get_title())
        toast.emit("button-clicked")
        self.run_until(lambda: len(self.server.posts) >= 2)
        path, body, allowed = self.server.posts[1]
        self.assertEqual(body["action"], "connect")
        self.assertEqual(body["plugs"][0]["plug"], "camera")
        self.assertEqual(body["slots"][0]["slot"], "camera")
        self.run_until(lambda: win.busy is False)

    def test_undo_reconnects_to_original_slot(self):
        # A plug connected to one of several compatible slots must undo
        # back to that same slot, not silently do nothing.
        conns = dict(CONNECTIONS)
        conns["slots"] = list(CONNECTIONS["slots"]) + [
            {"snap": "slot-provider", "slot": "camera",
             "interface": "camera", "connections": []}]
        conns["established"] = [
            {"slot": {"snap": "slot-provider", "slot": "camera"},
             "plug": {"snap": "firefox", "plug": "camera"},
             "interface": "camera", "manual": True}]
        win = self.load_win(connections=conns)
        row = self.switch_row(win, "camera")
        self.assertTrue(row.get_active())
        self.toggle_switch(row)
        self.confirm_alert("Disconnect", "confirm")
        self.wait_change_finished(win)
        toast = win.last_toast
        self.assertIn("Disconnected camera", toast.get_title())
        toast.emit("button-clicked")
        self.run_until(lambda: len(self.server.posts) >= 2)
        path, body, allowed = self.server.posts[1]
        self.assertEqual(body["action"], "connect")
        self.assertEqual(body["slots"][0],
                         {"snap": "slot-provider", "slot": "camera"})


    def test_undo_while_busy_shows_toast_single_post(self):
        # Undo on an old toast while a slow change runs: start_change
        # refuses the second change and shows a toast, exactly one POST.
        win = self.load_win()
        row = self.switch_row(win, "removable-media")
        self.toggle_switch(row)
        self.confirm_alert("Disconnect", "confirm")
        self.wait_change_finished(win)
        old_toast = win.last_toast
        self.assertIn("Disconnected removable-media", old_toast.get_title())
        self.assertEqual(len(self.server.posts), 1)
        self.server.delay = 0.5
        try:
            row2 = self.switch_row(win, "camera")
            self.toggle_switch(row2)
            self.confirm_alert("Disconnect camera", "confirm")
            self.run_until(lambda: win.busy is True)
            old_toast.emit("button-clicked")
            self.confirm_alert("Connect", "confirm")
            self.assertEqual(win.last_toast.get_title(),
                             "Another change is running")
            self.run_until(lambda: win.busy is False)
            self.assertEqual(len(self.server.posts), 2)
        finally:
            self.server.delay = 0

    def test_undo_without_baseline_shows_toast_no_post(self):
        # The baseline gate lives in start_change: losing the
        # baseline while an undo toast is open blocks the undo.
        win = self.load_win()
        row = self.switch_row(win, "camera")
        self.toggle_switch(row)
        self.confirm_alert("Disconnect", "confirm")
        self.wait_change_finished(win)
        toast = win.last_toast
        self.assertIn("Disconnected camera", toast.get_title())
        self.assertEqual(len(self.server.posts), 1)
        for name in ("baseline.json", "baseline.json.bak"):
            with open(os.path.join(os.environ["GINGER_DATA_DIR"],
                                   name), "w") as f:
                f.write("not-json{")
        win.load()
        self.assertTrue(win.baseline_banner.get_revealed())
        toast.emit("button-clicked")
        self.run_until(lambda: win.last_toast is not toast)
        self.assertEqual(len(self.server.posts), 1)
        self.assertIn("original state", win.last_toast.get_title())

    def rename_plug(self, conns, plug, rename):
        for p in conns["plugs"]:
            if p["plug"] == plug:
                p["plug"] = rename
        for e in conns["established"]:
            if e["plug"]["plug"] == plug:
                e["plug"]["plug"] = rename
        return conns

    def test_markup_in_connect_confirmation_not_parsed(self):
        plug = "<b>x</b>&amp;"
        conns = copy.deepcopy(CONNECTIONS)
        conns["established"] = [e for e in conns["established"]
                                if e["plug"]["plug"] != "removable-media"]
        conns["undesired"].append(
            {"slot": {"snap": "snapd", "slot": "removable-media"},
             "plug": {"snap": "firefox", "plug": plug},
             "interface": "removable-media", "manual": True})
        self.rename_plug(conns, "removable-media", plug)
        win = self.load_win(connections=conns)
        row = self.switch_row(win, plug)
        self.assertFalse(row.get_active())
        self.toggle_switch(row)
        alert = self.wait_alert("Connect")
        self.assertIn(plug, alert.get_heading())
        self.assertFalse(alert.get_heading_use_markup())
        self.assertIn(plug, alert.get_body())
        self.assertFalse(alert.get_body_use_markup())
        alert.emit("response", "cancel")

    def test_markup_in_disconnect_confirmation_not_parsed(self):
        plug = "<b>x</b>&amp;"
        conns = copy.deepcopy(CONNECTIONS)
        self.rename_plug(conns, "camera", plug)
        win = self.load_win(connections=conns)
        row = self.switch_row(win, plug)
        self.assertTrue(row.get_active())
        self.toggle_switch(row)
        alert = self.wait_alert("Disconnect")
        self.assertIn(plug, alert.get_heading())
        self.assertFalse(alert.get_heading_use_markup())
        self.assertIn(plug, alert.get_body())
        self.assertFalse(alert.get_body_use_markup())
        alert.emit("response", "cancel")

    def test_change_done_while_closing_does_nothing(self):
        win = self.load_win()
        win.on_close_request()
        self.assertTrue(win.closing)
        self.assertIsNone(win.last_toast)
        win.change_done("disconnect", "firefox", "camera",
                        ("snapd", "camera"), changes.OUTCOME_DONE, None)
        self.assertIsNone(win.last_toast)
        self.assertEqual(self.server.posts, [])

    def test_transient_poll_error_shows_state_unknown(self):
        # A transient error while polling (for example a read timeout)
        # reloads and shows the unknown-state toast, not an error dialog.
        win = self.load_win()
        orig = win.client.get_change
        calls = []

        def flaky(cid):
            calls.append(cid)
            if len(calls) > 1:
                raise snapd_client.SnapdError("transient",
                                              kind="request-timeout")
            return {"status": "Doing", "ready": False, "err": None}
        win.client.get_change = flaky
        try:
            row = self.switch_row(win, "camera")
            self.toggle_switch(row)
            self.confirm_alert("Disconnect", "confirm")
            self.wait_change_finished(win)
        finally:
            win.client.get_change = orig
        self.assertEqual(win.last_toast.get_title(),
                         "State unknown, reloaded")
        # A dialog is only closed by AdwDialog.close(); emitting
        # ::response completes choose() but leaves the dialog open.
        alerts = [d for w in Gtk.Window.list_toplevels()
                  for d in self.walk(w) if isinstance(d, Adw.AlertDialog)]
        self.assertTrue(all(d.get_heading() != "snapd returned an error"
                             for d in alerts), alerts)
        self.assertTrue(self.switch_row(win, "camera").get_active())


class RestoreTests(UISmokeTests):
    def baseline_for(self, connections, snap="firefox"):
        import baseline as baseline_mod
        return baseline_mod.entry_for(connections, snap)

    def write_baseline(self, snap, entry):
        import json as json_mod
        path = os.path.join(os.environ["GINGER_DATA_DIR"],
                            "baseline.json")
        data = {"version": 2, "snaps": {snap: entry}}
        os.makedirs(os.environ["GINGER_DATA_DIR"], exist_ok=True)
        with open(path, "w") as f:
            json_mod.dump(data, f)
        with open(path + ".bak", "w") as f:
            json_mod.dump(data, f)

    def changed_connections(self):
        # camera disconnected (was in baseline), removable-media
        # connected manually (was not in baseline).
        conns = copy.deepcopy(CONNECTIONS)
        conns["established"] = [
            e for e in conns["established"]
            if e["plug"]["plug"] != "camera"]
        conns["established"].append({
            "slot": {"snap": "snapd", "slot": "camera"},
            "plug": {"snap": "firefox", "plug": "camera"},
            "interface": "camera", "manual": True})
        return conns

    def base_conns_with_baseline(self, conns):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = conns
        self.write_baseline("firefox", self.baseline_for(conns))
        win = self.make_window()
        win.on_snap_selected(win.snaps_list,
                             win.snaps_list.get_row_at_index(0))
        return win

    def test_banner_row_only_for_changed_snap(self):
        conns = CONNECTIONS
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = conns
        self.write_baseline("firefox", self.baseline_for(conns))
        win = self.make_window()
        win.on_snap_selected(win.snaps_list,
                             win.snaps_list.get_row_at_index(0))
        titles = [r.get_title() for r in self.walk(win.detail_bin.get_child())
                   if isinstance(r, Adw.ActionRow)]
        self.assertNotIn("This snap differs from its original state", titles)
        win = self.base_conns_with_baseline(self.changed_connections())
        titles = [r.get_title() for r in self.walk(win.detail_bin.get_child())
                  if isinstance(r, Adw.ActionRow)]
        self.assertIn("This snap differs from its original state", titles)

    def test_modified_icon_and_only_changed_filter(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        row = win.snaps_list.get_row_at_index(0)
        icons = [w for w in self.walk(row)
                 if isinstance(w, Gtk.Image)
                 and w.get_icon_name() == "document-modified-symbolic"]
        self.assertTrue(icons)
        self.assertFalse(win.only_changed)
        win.changed_check.set_active(True)
        self.assertTrue(win.only_changed)
        self.assertEqual([t[0] for t in self.row_texts(win)], ["firefox"])
        # Unchanged snap is filtered out.
        self.write_baseline("firefox", self.baseline_for(
            self.server.default_connections))
        win.load()
        self.assertEqual(self.row_texts(win), [])

    def test_restore_page_rows_and_buttons(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        win.open_restore_page()
        self.assertEqual(win.page_stack.get_visible_child_name(), "restore")
        rows = [r for r in win.walk(win.restore_rows)
                if isinstance(r, Adw.ActionRow)]
        self.assertEqual([r.get_title() for r in rows], ["firefox"])
        self.assertIn("steps", rows[0].get_subtitle())
        self.assertFalse(win.restore_selected_button.get_sensitive())
        self.assertTrue(win.restore_all_button.get_sensitive())
        check = win.restore_checks["firefox"]
        check.set_active(True)
        self.assertTrue(win.restore_selected_button.get_sensitive())

    def test_restore_page_empty_state(self):
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        self.write_baseline("firefox", self.baseline_for(CONNECTIONS))
        win = self.make_window()
        win.open_restore_page()
        rows = [r for r in win.walk(win.restore_rows)
                if isinstance(r, Adw.ActionRow)]
        self.assertEqual([r.get_title() for r in rows],
                         ["Nothing to restore"])
        self.assertFalse(win.restore_all_button.get_sensitive())

    def test_hamburger_restore_all_opens_page_only(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        win.activate_action("win.restore-page", None)
        self.run_until(lambda: win.page_stack.get_visible_child_name()
                       == "restore")
        self.assertEqual(self.server.posts, [])

    def restore_alert(self, win, heading="Restore original connections?"):
        ctx = GLib.MainContext.default()
        end = GLib.get_monotonic_time() + 3000 * 1000
        alert = []
        while not alert and GLib.get_monotonic_time() < end:
            alert = [d for w in Gtk.Window.list_toplevels()
                     for d in self.walk(w)
                     if isinstance(d, Adw.AlertDialog)
                     and heading in d.get_heading()]
            self.assertEqual(len(alert), 1,
                             "expected exactly one restore alert")
            if not alert and not ctx.iteration(False):
                time.sleep(0.01)
        self.assertTrue(alert, "no restore alert appeared")
        return alert[0]

    OUTSIDE_GINGER_SENTENCE = (
        "Restore also reverts changes made outside Ginger since the "
        "saved original state (for example with snap connect).")

    def test_confirmation_dialog_contents_one_snap(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        titles = [r.get_title() for r in self.walk(win.detail_bin.get_child())
                  if isinstance(r, Adw.ActionRow)]
        restore_row = next(r for r in
                           self.walk(win.detail_bin.get_child())
                           if isinstance(r, Adw.ActionRow)
                           and r.get_title()
                           == "This snap differs from its original state")
        restore_row.get_activatable_widget().emit("clicked")
        alert = self.restore_alert(win)
        body = alert.get_body()
        self.assertEqual(body.count(self.OUTSIDE_GINGER_SENTENCE), 1)
        self.assertIn("Connect firefox:camera", body)
        self.assertIn("Disconnect firefox:removable-media", body)
        self.assertIn("sensitive interface", body)
        self.assertIn("This will ask for approval 2 times.", body)
        self.assertFalse(alert.get_body_use_markup())
        alert.emit("response", "cancel")
        self.assertEqual(self.server.posts, [])

    def test_confirmation_dialog_contents_all_snaps(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        win.open_restore_page()
        win.restore_all_button.emit("clicked")
        alert = self.restore_alert(win)
        body = alert.get_body()
        self.assertEqual(body.count(self.OUTSIDE_GINGER_SENTENCE), 1)
        self.assertIn("This will ask for approval 2 times.", body)
        alert.emit("response", "cancel")

    def test_restore_batch_success_and_convergence(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        win.open_restore_page()
        win.restore_all_button.emit("clicked")
        alert = self.restore_alert(win)
        alert.emit("response", "restore")
        self.run_until(lambda: win.restore_batch is None
                       and win.busy is False)
        self.assertEqual(len(self.server.posts), 2)
        for path, body, allowed in self.server.posts:
            self.assertEqual(path, "/v2/interfaces")
            self.assertTrue(allowed)
        # The mock applied the connects: the diff is now empty.
        import restore as restore_mod
        diff = restore_mod.compute_diff(
            self.baseline_for(self.server.default_connections),
            self.server.default_connections, "firefox")
        self.assertEqual(diff["steps"], [])
        self.assertEqual(win.changed_snaps(), {})

    def test_restore_cancelled_mid_batch(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        win.open_restore_page()
        self.server.interface_responses = [
            (200, {"type": "sync", "status-code": 200, "result": None}),
            (403, {"type": "error", "status-code": 403,
                   "result": {"message": "cancelled",
                              "kind": "auth-cancelled"}})]
        win.restore_all_button.emit("clicked")
        alert = self.restore_alert(win)
        alert.emit("response", "restore")
        self.run_until(lambda: win.restore_batch is None)
        self.assertEqual(len(self.server.posts), 2)
        self.assertIn("Stopped: 1 of 2 steps done",
                      win.last_toast.get_title())

    def test_restore_error_mid_batch(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        win.open_restore_page()
        self.server.interface_responses = [
            (200, {"type": "sync", "status-code": 200, "result": None}),
            (500, {"type": "error", "status-code": 500,
                   "result": {"message": "snapd exploded",
                              "kind": "internal-error"}})]
        win.restore_all_button.emit("clicked")
        alert = self.restore_alert(win)
        alert.emit("response", "restore")
        self.run_until(lambda: win.restore_batch is None)
        self.assertIn("1 of 2", win.last_toast.get_title())
        self.assertIn("snapd exploded", win.last_toast.get_title())

    def test_restore_busy_guard_blocks_second_change(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        self.server.delay = 0.5
        try:
            row = self.switch_row(win, "network")
            self.toggle_switch(row)
            self.confirm_alert("Disconnect network", "confirm")
            self.run_until(lambda: win.busy is True)
            win.open_restore_page()
            win.restore_all_button.emit("clicked")
            # confirm_restore is refused while busy: no alert appears.
            alerts = [d for w in Gtk.Window.list_toplevels()
                      for d in self.walk(w)
                      if isinstance(d, Adw.AlertDialog)
                      and "Restore original" in d.get_heading()]
            self.assertEqual(alerts, [])
            self.run_until(lambda: win.busy is False)
        finally:
            self.server.delay = 0

    def test_restore_disabled_without_baseline(self):
        conns = self.changed_connections()
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = conns
        win = self.make_window()
        win.on_snap_selected(win.snaps_list,
                             win.snaps_list.get_row_at_index(0))
        win.open_restore_page()
        rows = [r for r in win.walk(win.restore_rows)
                if isinstance(r, Adw.ActionRow)]
        self.assertEqual([r.get_title() for r in rows],
                         ["Nothing to restore"])

    def test_restore_markup_names_render_literally(self):
        plug = "<b>x</b>&amp;"
        conns = self.changed_connections()
        conns = copy.deepcopy(conns)
        for p in conns["plugs"]:
            if p["plug"] == "camera":
                p["plug"] = plug
        for e in conns["established"]:
            if e["plug"]["plug"] == "camera":
                e["plug"]["plug"] = plug
        conns["established"] = [e for e in conns["established"]
                                if e["plug"]["plug"] != plug]
        snap = dict(SNAP_APP, name="<snap>&amp;")
        for p in conns["plugs"]:
            p["snap"] = snap["name"]
        for e in conns["established"] + conns["undesired"]:
            e["plug"]["snap"] = snap["name"]
        self.server.snaps = [snap]
        self.server.default_connections = conns
        self.write_baseline(snap["name"],
                            self.baseline_for(conns, snap["name"]))
        win = self.make_window()
        win.on_snap_selected(win.snaps_list,
                             win.snaps_list.get_row_at_index(0))
        titles = [r.get_title() for r in self.walk(win.detail_bin.get_child())
                  if isinstance(r, Adw.ActionRow)]
        self.assertIn("This snap differs from its original state", titles)
        win.open_restore_page()
        rows = [r for r in win.walk(win.restore_rows)
                if isinstance(r, Adw.ActionRow)]
        self.assertEqual(rows[0].get_title(), snap["name"])
        win.restore_all_button.emit("clicked")
        alert = self.restore_alert(win)
        self.assertIn(plug, alert.get_body())
        alert.emit("response", "cancel")

    def test_restore_window_closing_mid_batch(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        win.on_close_request()
        win.restore_batch = {"snaps": ["firefox"], "steps": [
            {"action": "connect", "plug_snap": "firefox",
             "plug": "camera", "slot_snap": "snapd", "slot": "camera",
             "tier": 1}], "index": 0}
        win.change_done("connect", "firefox", "camera",
                        ("snapd", "camera"), changes.OUTCOME_DONE, None)
        self.assertIsNone(win.last_toast)

    def test_restore_page_pickable(self):
        win = self.base_conns_with_baseline(self.changed_connections())
        win.open_restore_page()
        self.run_until(lambda: win.get_mapped()
                       and win.restore_rows.get_width() > 1)
        point = Graphene.Point()
        point.x = win.restore_rows.get_width() / 2
        point.y = min(win.restore_rows.get_height() / 2, 5)
        result = win.restore_rows.compute_point(win, point)
        ok, out = result if isinstance(result, tuple) else (True, result)
        self.assertTrue(ok)
        widget = win.pick(out.x, out.y, Gtk.PickFlags.DEFAULT)
        self.assertIsNotNone(widget)
        self.assertTrue(widget is win.restore_rows
                        or widget.is_ancestor(win.restore_rows),
                        "picked %s" % widget)



class PointerPickTests(unittest.TestCase):
    """win.pick exercises pointer picking; emitting signals does not.

    A widget covering a pane (an overlay child) is picked before the
    content under it even when it draws nothing.
    """

    @classmethod
    def setUpClass(cls):
        cls.main, cls.app = shared_app()
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.socket_path = os.path.join(cls.tmpdir.name, "snapd.socket")
        cls.server = MockSnapd(cls.socket_path)
        os.environ["SNAPD_SOCKET"] = cls.socket_path

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.tmpdir.cleanup()

    def setUp(self):
        self.main, self.app = shared_app()
        self.server.snaps = [dict(SNAP_APP)]
        self.server.default_connections = CONNECTIONS
        self.server.posts = []
        self.server.responses = []
        self.server.interface_responses = []
        self.server.change_script = {}
        self.server.next_change_id = 1
        self.server.delay = 0
        self.prev_data_dir = os.environ.get("GINGER_DATA_DIR")
        self.tmpdir_i = tempfile.TemporaryDirectory()
        os.environ["GINGER_DATA_DIR"] = self.tmpdir_i.name
        self.win = self.main.Window(self.app)
        self.win.present()
        self.win.load()
        self.win.on_snap_selected(self.win.snaps_list,
                                  self.win.snaps_list.get_row_at_index(0))
        self.run_until(lambda: self.win.get_mapped()
                        and self.win.snaps_list.get_width() > 1
                        and self.win.detail_pane.get_width() > 1)

    def tearDown(self):
        self.win.destroy()
        self.tmpdir_i.cleanup()
        if self.prev_data_dir is None:
            del os.environ["GINGER_DATA_DIR"]
        else:
            os.environ["GINGER_DATA_DIR"] = self.prev_data_dir

    def run_until(self, condition, timeout_ms=3000):
        ctx = GLib.MainContext.default()
        end = GLib.get_monotonic_time() + timeout_ms * 1000
        while not condition() and GLib.get_monotonic_time() < end:
            if not ctx.iteration(False):
                time.sleep(0.01)
        self.assertTrue(condition(), "run_until timed out")

    def point_in(self, target):
        point = Graphene.Point()
        point.x = target.get_width() / 2
        point.y = min(target.get_height() / 2, 5)
        result = target.compute_point(self.win, point)
        if isinstance(result, tuple):
            ok, out = result
        else:
            ok, out = True, result
        self.assertTrue(ok, "compute_point failed")
        return out.x, out.y

    def pick(self, target):
        x, y = self.point_in(target)
        return self.win.pick(x, y, Gtk.PickFlags.DEFAULT)

    def assert_picks_inside(self, target, pane, banner_hidden):
        widget = self.pick(target)
        self.assertIsNotNone(widget)
        self.assertTrue(widget.is_ancestor(pane) or widget is pane,
                        "picked %s, expected inside %s"
                        % (widget, pane))
        self.assertFalse(widget is self.win.baseline_banner
                         or widget.is_ancestor(self.win.baseline_banner),
                         "picked the banner over the pane")
        self.assertEqual(self.win.baseline_banner.get_revealed(),
                         not banner_hidden)

    def test_list_and_detail_pickable_banner_hidden(self):
        self.assertFalse(self.win.baseline_banner.get_revealed())
        self.assert_picks_inside(self.win.snaps_list, self.win.snaps_list,
                                 banner_hidden=True)
        self.assert_picks_inside(self.win.detail_pane, self.win.detail_pane,
                                 banner_hidden=True)

    def test_list_and_detail_pickable_banner_revealed(self):
        self.win.baseline_banner.set_revealed(True)
        self.run_until(lambda: self.win.baseline_banner.get_height() > 1)
        self.assert_picks_inside(self.win.snaps_list, self.win.snaps_list,
                                 banner_hidden=False)
        self.assert_picks_inside(self.win.detail_pane, self.win.detail_pane,
                                 banner_hidden=False)

    def test_header_buttons_pickable(self):
        picked = 0
        for button in (self.win.sidebar_toggle, self.win.search_button,
                       self.win.filter_button):
            # The sidebar toggle is visible only when collapsed.
            if not button.get_visible():
                continue
            picked += 1
            widget = self.pick(button)
            self.assertIsNotNone(widget)
            self.assertTrue(widget is button or widget.is_ancestor(button),
                            "picked %s, expected %s" % (widget, button))
        self.assertGreaterEqual(picked, 2)

    def test_unrevealed_banner_takes_no_space(self):
        self.assertFalse(self.win.baseline_banner.get_revealed())
        self.assertEqual(self.win.baseline_banner.get_height(), 0)


class PackagingTests(unittest.TestCase):
    def test_app_id_and_version_consistent(self):
        root = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(root, "main.py")) as f:
            main_src = f.read()
        self.assertIn('APP_ID = "io.github.massivemarmot.Ginger"', main_src)
        m = re.search(r'VERSION = "([0-9.]+)"', main_src)
        self.assertIsNotNone(m)
        version = m.group(1)
        with open(os.path.join(root,
                               "io.github.massivemarmot.Ginger.yml")) as f:
            manifest = f.read()
        self.assertIn("io.github.massivemarmot.Ginger", manifest)
        with open(os.path.join(
                root, "data/io.github.massivemarmot.Ginger.desktop")) as f:
            desktop = f.read()
        self.assertIn("Icon=io.github.massivemarmot.Ginger", desktop)
        with open(os.path.join(
                root,
                "data/io.github.massivemarmot.Ginger.metainfo.xml")) as f:
            metainfo = f.read()
        self.assertIn("<id>io.github.massivemarmot.Ginger</id>", metainfo)
        self.assertIn('version="%s"' % version, metainfo)
        self.assertIn(
            '<launchable type="desktop-id">'
            "io.github.massivemarmot.Ginger.desktop</launchable>", metainfo)


if __name__ == "__main__":
    unittest.main()
