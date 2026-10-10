# SPDX-License-Identifier: GPL-3.0-or-later
import os
import sys
import threading

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

import baseline
import changes
import interfaces
import restore
from snapd_client import Client, SnapdError

APP_ID = "io.github.massivemarmot.Ginger"
VERSION = "0.1.0"
REPO_URL = "https://github.com/MassiveMarmot/Ginger"
FUNNEL_ICON = "funnel-symbolic"

ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "icons")


def register_icon_search_path():
    if not os.path.isdir(ICONS_DIR):
        return
    theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
    theme.add_search_path(ICONS_DIR)
    if not theme.has_icon(FUNNEL_ICON):
        print("Failed to load icon %s from %s" % (FUNNEL_ICON, ICONS_DIR),
              file=sys.stderr)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Ginger",
                         default_width=921, default_height=450)
        self.client = Client()
        self.snaps = []
        self.selected_snap = None
        self.query = ""
        self.show_libraries = False
        self.show_all_interfaces = False
        self.only_changed = False
        self.connections = {}
        self.connections_by_snap = {}
        self.baselines = {}
        self.baseline_problem = None
        self.busy = False
        self.confirming = False
        self.closing = False
        self.suppress_switch_handler = False
        self.last_toast = None
        self.restore_batch = None

        self.sidebar_rows = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.sidebar_rows.connect("row-activated", self.on_page_selected)

        sidebar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar.add_top_bar(sidebar_header)
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(self.sidebar_rows)
        sidebar.set_content(scroller)

        refresh = Gio.SimpleAction.new("refresh", None)
        refresh.connect("activate", lambda *a: self.load())
        self.add_action(refresh)
        sidebar_header.pack_start(Gtk.Button(
            icon_name="view-refresh-symbolic", action_name="win.refresh",
            tooltip_text="Refresh"))

        menu = Gio.Menu()
        menu.append("About", "win.about")
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", lambda *a: self.show_about())
        self.add_action(about)
        sidebar_header.pack_end(Gtk.MenuButton(
            icon_name="open-menu-symbolic", menu_model=menu,
            tooltip_text="Main Menu"))

        self.main_split = Adw.OverlaySplitView(
            collapsed=False, show_sidebar=True, min_sidebar_width=250)

        self.snaps_page = self.build_snaps_page()
        self.restore_page = self.build_restore_page()
        self.error_page = Adw.StatusPage(icon_name="network-error-symbolic")
        self.error_wrapper = self.page_with_header(self.error_page)

        self.page_stack = Gtk.Stack(vhomogeneous=False)
        self.page_stack.add_named(self.snaps_page, "snaps")
        self.page_stack.add_named(self.restore_page, "restore")
        self.page_stack.add_named(self.error_wrapper, "error")

        self.baseline_banner = Adw.Banner(
            title="The saved original state cannot be read",
            button_label="Start a new saved state",
            valign=Gtk.Align.START)
        self.baseline_banner.connect("button-clicked",
                                     lambda *a: self.confirm_new_baseline())
        self.toast_overlay = Adw.ToastOverlay(vexpand=True,
                                               hexpand=True)
        self.toast_overlay.set_child(self.page_stack)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content.append(self.baseline_banner)
        content.append(self.toast_overlay)

        self.build_sidebar()
        self.sidebar_rows.select_row(self.sidebar_rows.get_row_at_index(0))

        self.main_split.set_sidebar(sidebar)
        self.main_split.set_content(content)
        self.set_content(self.main_split)

        self.setup_breakpoints()
        self.connect("close-request", self.on_close_request)
        self.on_page_selected(self.sidebar_rows,
                              self.sidebar_rows.get_row_at_index(0))

    def on_close_request(self, *args):
        self.closing = True
        return False

    def page_with_header(self, page):
        header = Adw.HeaderBar()
        header.pack_start(self.make_sidebar_toggle())
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(page)
        view.set_content(scroller)
        return view

    def make_sidebar_toggle(self):
        button = Gtk.ToggleButton(
            icon_name="sidebar-show-symbolic", tooltip_text="Show Sidebar")
        self.main_split.bind_property(
            "show-sidebar", button, "active",
            GObject.BindingFlags.BIDIRECTIONAL
            | GObject.BindingFlags.SYNC_CREATE)
        self.main_split.bind_property(
            "collapsed", button, "visible",
            GObject.BindingFlags.SYNC_CREATE)
        return button

    def build_sidebar(self):
        self.sidebar_rows.remove_all()
        for name, icon in (("Snaps", "application-x-executable-symbolic"),
                           ("Restore", "document-revert-symbolic")):
            row = Gtk.ListBoxRow()
            row.page_name = name
            box = Gtk.Box(margin_top=12, margin_bottom=12,
                          margin_start=6, margin_end=6, spacing=12)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=name, xalign=0, hexpand=True,
                                 use_markup=False))
            row.set_child(box)
            self.sidebar_rows.append(row)

    def show_about(self):
        Adw.AboutDialog(application_name="Ginger",
                        application_icon=APP_ID, version=VERSION,
                        comments="Ginger for Snaps",
                        website=REPO_URL,
                        issue_url=REPO_URL + "/issues",
                        license_type=Gtk.License.GPL_3_0).present(self)

    def on_page_selected(self, box, row):
        if self.main_split.get_collapsed():
            self.main_split.set_show_sidebar(False)
        name = getattr(row, "page_name", "Snaps") if row is not None \
            else "Snaps"
        if name == "Restore":
            self.page_stack.set_visible_child_name("restore")
        else:
            self.page_stack.set_visible_child_name("snaps")

    def open_restore_page(self, *args):
        self.sidebar_rows.select_row(self.sidebar_rows.get_row_at_index(1))
        self.on_page_selected(self.sidebar_rows,
                              self.sidebar_rows.get_row_at_index(1))

    def snap_names(self):
        names = {}
        for s in self.snaps:
            if isinstance(s, dict) and s.get("type") in (None, "app"):
                snap = dict(s)
                snap["has_apps"] = bool(s.get("apps"))
                names[str(s.get("name") or "?")] = snap
        return names

    def visible_snaps(self):
        out = []
        changed = self.changed_snaps()
        for name in sorted(self.snap_map):
            snap = self.snap_map[name]
            if not self.show_libraries and not snap.get("has_apps"):
                continue
            if self.query and self.query.lower() not in name.lower():
                continue
            if self.only_changed and name not in changed:
                continue
            out.append(name)
        return out

    def build_snaps_page(self):
        self.list_header = Adw.HeaderBar()
        self.search_button = Gtk.ToggleButton(
            icon_name="system-search-symbolic", tooltip_text="Search")
        self.search_button.connect("toggled", self.on_search_toggled)
        self.sidebar_toggle = self.make_sidebar_toggle()
        self.list_header.pack_start(self.sidebar_toggle)
        self.list_header.pack_start(self.search_button)

        self.list_title = Gtk.Label(
            label="Snaps", css_classes=["heading"], hexpand=True)
        self.list_header.set_title_widget(self.list_title)

        self.filter_button = Gtk.ToggleButton(
            icon_name=FUNNEL_ICON, tooltip_text="Filter")
        self.filter_button.connect("toggled", self.on_filter_toggled)
        self.list_header.pack_end(self.filter_button)

        self.search_bar = Gtk.SearchBar(hexpand=True)
        self.search_entry = Gtk.SearchEntry(hexpand=True)
        self.search_entry.connect("search-changed", self.on_search_changed)
        self.search_bar.set_child(self.search_entry)

        self.snaps_list = Gtk.ListBox(
            css_classes=["navigation-sidebar"], activate_on_single_click=True)
        self.snaps_list.connect("row-activated", self.on_snap_selected)

        self.filter_panel = self.build_filter_panel()

        list_view = Adw.ToolbarView()
        list_view.add_top_bar(self.list_header)
        list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        list_box.append(self.search_bar)
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(self.snaps_list)
        list_box.append(scroller)
        list_view.set_content(list_box)

        self.detail_pane = Adw.ToolbarView()
        detail_header = Adw.HeaderBar(show_title=False)
        self.detail_pane.add_top_bar(detail_header)
        self.detail_bin = Adw.Bin()
        scroller2 = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller2.set_child(self.detail_bin)
        self.detail_pane.set_content(scroller2)

        list_page = Adw.NavigationPage(title="Snaps")
        list_page.set_child(list_view)
        self.detail_page = Adw.NavigationPage(title="Snap")
        self.detail_page.set_child(self.detail_pane)
        self.detail_split = Adw.NavigationSplitView(
            sidebar_width_fraction=0.5)
        self.detail_split.set_sidebar(list_page)
        self.detail_split.set_content(self.detail_page)
        return self.detail_split

    def build_filter_panel(self):
        lib_check = Gtk.CheckButton(css_classes=["selection-mode"])
        lib_check.connect("toggled", self.on_lib_check_toggled)
        lib_row = Adw.ActionRow(title="Show libraries and runtimes",
                                use_markup=False)
        lib_row.add_suffix(lib_check)
        lib_row.set_activatable_widget(lib_check)
        all_check = Gtk.CheckButton(css_classes=["selection-mode"])
        all_check.connect("toggled", self.on_all_check_toggled)
        all_row = Adw.ActionRow(title="Show all interfaces",
                                use_markup=False)
        all_row.add_suffix(all_check)
        all_row.set_activatable_widget(all_check)
        changed_check = Gtk.CheckButton(css_classes=["selection-mode"])
        changed_check.connect("toggled", self.on_changed_check_toggled)
        changed_row = Adw.ActionRow(title="Only changed snaps",
                                   use_markup=False)
        changed_row.add_suffix(changed_check)
        changed_row.set_activatable_widget(changed_check)
        group = Adw.PreferencesGroup()
        group.add(lib_row)
        group.add(all_row)
        group.add(changed_row)
        self.lib_check = lib_check
        self.all_check = all_check
        self.changed_check = changed_check
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)
        scroller = Gtk.ScrolledWindow()
        scroller.set_child(group)
        view.set_content(scroller)
        return view

    def build_restore_page(self):
        self.restore_checks = None
        self.restore_rows = Gtk.ListBox(
            css_classes=["navigation-sidebar"])
        self.restore_selected_button = Gtk.Button(
            label="Restore selected")
        self.restore_selected_button.connect(
            "clicked", lambda *a: self.on_restore_selected())
        self.restore_all_button = Gtk.Button(label="Restore all")
        self.restore_all_button.connect(
            "clicked", lambda *a: self.confirm_restore(None))
        header = Adw.HeaderBar()
        header.pack_start(self.make_sidebar_toggle())
        header.pack_end(self.restore_all_button)
        header.pack_end(self.restore_selected_button)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_child(self.restore_rows)
        view.set_content(scroller)
        page = Adw.NavigationPage(title="Restore")
        page.set_child(view)
        return page

    def refresh_restore_page(self):
        self.restore_rows.remove_all()
        self.restore_checks = {}
        changed = self.changed_snaps()
        for snap in sorted(changed):
            row = Adw.ActionRow(title=snap, use_markup=False)
            check = Gtk.CheckButton(css_classes=["selection-mode"])
            self.restore_checks[snap] = check
            row.add_suffix(check)
            row.set_activatable_widget(check)
            steps = restore.compute_diff(
                self.baselines.get(snap),
                self.connections_by_snap.get(snap) or {}, snap)
            row.set_subtitle("%d steps" % len(steps["steps"]))
            self.restore_rows.append(row)
        if not changed:
            self.restore_rows.append(Adw.ActionRow(
                title="Nothing to restore", use_markup=False))
        self.restore_selected_button.set_sensitive(any(
            c.get_active() for c in self.restore_checks.values()))
        self.restore_all_button.set_sensitive(bool(changed))

    def changed_snaps(self):
        if self.baseline_problem is not None:
            return {}
        return restore.changed_snaps(
            self.baselines, self.connections_by_snap)

    def on_restore_selected(self):
        snaps = sorted(snap for snap, check in self.restore_checks.items()
                       if check.get_active())
        self.confirm_restore(snaps)

    def on_lib_check_toggled(self, check):
        self.show_libraries = check.get_active()
        self.refresh_list()

    def on_all_check_toggled(self, check):
        self.show_all_interfaces = check.get_active()
        self.update_detail()

    def on_changed_check_toggled(self, check):
        self.only_changed = check.get_active()
        self.refresh_list()

    def on_filter_toggled(self, button):
        active = button.get_active()
        self.detail_page.set_child(
            self.filter_panel if active else self.detail_pane)
        if active:
            self.detail_split.set_show_content(True)
        self.refresh_list()

    def on_search_toggled(self, button):
        self.search_bar.set_search_mode(button.get_active())

    def on_search_changed(self, entry):
        self.query = entry.get_text()
        self.refresh_list()

    def refresh_list(self):
        self.snap_map = self.snap_names()
        self.snaps_list.remove_all()
        self.rows_by_name = {}
        for name in self.visible_snaps():
            row = self.snap_row(name)
            self.rows_by_name[name] = row
            self.snaps_list.append(row)
        self.update_detail()
        self.highlight_selected_row()
        self.refresh_restore_page()

    def snap_row(self, name):
        connections = self.connections_by_snap.get(name)
        subtitle = ""
        if connections is not None:
            connected, available = interfaces.counts(connections, name)
            subtitle = "%d connected, %d available" % (connected, available)
        row = Adw.ActionRow(title=name, subtitle=subtitle, use_markup=False)
        row.set_activatable(True)
        row.snap_name = name
        row.add_prefix(Gtk.Image(icon_name="application-x-executable-symbolic"))
        if name in self.changed_snaps():
            row.add_suffix(Gtk.Image(icon_name="document-modified-symbolic",
                                     tooltip_text="Modified"))
        return row

    def highlight_selected_row(self):
        row = self.rows_by_name.get(self.selected_snap)
        if row is not None:
            self.snaps_list.select_row(row)

    def on_snap_selected(self, box, row, from_user=True):
        if row is None or not hasattr(row, "snap_name"):
            return
        self.selected_snap = row.snap_name
        if self.filter_button.get_active():
            self.filter_button.set_active(False)
        if from_user:
            self.detail_split.set_show_content(True)
        self.update_detail()
        self.highlight_selected_row()

    def update_detail(self):
        snap = self.snap_map.get(self.selected_snap)
        if snap is None:
            self.detail_bin.set_child(Adw.StatusPage(
                title="No snap selected",
                icon_name="document-open-symbolic"))
            return
        name = self.selected_snap
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=24, margin_bottom=24,
                      margin_start=12, margin_end=12,
                      valign=Gtk.Align.CENTER)
        box.append(Gtk.Image(
            icon_name="application-x-executable-symbolic",
            pixel_size=96, valign=Gtk.Align.START))
        title = Gtk.Label(css_classes=["title-2"], use_markup=False)
        title.set_text(name)
        box.append(title)
        if snap.get("summary"):
            summary = Gtk.Label(css_classes=["dim-label"],
                                ellipsize=Pango.EllipsizeMode.END,
                                use_markup=False)
            summary.set_text(str(snap.get("summary")))
            box.append(summary)
        card = Adw.PreferencesGroup()
        snap_name_row = Adw.ActionRow(title="Snap name", use_markup=False,
                                      css_classes=["property"])
        snap_name_row.set_subtitle(name)
        copy = Gtk.Button(icon_name="edit-copy-symbolic",
                          css_classes=["flat"])
        copy.connect("clicked", self.copy_text, name)
        snap_name_row.add_suffix(copy)
        snap_name_row.set_activatable_widget(copy)
        card.add(snap_name_row)
        if snap.get("version"):
            version_row = Adw.ActionRow(
                title="Version", use_markup=False,
                css_classes=["property"],
                subtitle=str(snap.get("version")))
            card.add(version_row)
        box.append(card)
        connections = self.connections_by_snap.get(name)
        if connections is not None:
            if name in self.changed_snaps():
                banner_row = Adw.ActionRow(
                    title="This snap differs from its original state",
                    use_markup=False)
                restore_button = Gtk.Button(label="Restore original…")
                restore_button.connect(
                    "clicked", lambda *a: self.confirm_restore([name]))
                banner_row.add_suffix(restore_button)
                banner_row.set_activatable_widget(restore_button)
                box.append(banner_row)
            box.append(self.permissions_group(name, connections))
        self.detail_bin.set_child(box)

    def confirm_new_baseline(self):
        alert = Adw.AlertDialog(
            heading="Start a new saved state?",
            body="The unreadable files are kept on disk. The new saved "
                 "state is taken from the connections as they are now, "
                 "so it will no longer be the state from before Ginger "
                 "was first used.")
        alert.add_response("cancel", "Cancel")
        alert.add_response("restart", "Start a new saved state")
        alert.set_response_appearance(
            "restart", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.choose(self, None, self.on_new_baseline_confirmed, None)

    def on_new_baseline_confirmed(self, source, result, _):
        if source.choose_finish(result) != "restart":
            return
        import time
        baseline.quarantine_unreadable(
            time.strftime("%Y%m%d-%H%M%S"))
        self.load()

    def restore_diff_for(self, snaps):
        steps, skipped, not_restored = [], [], []
        for snap in snaps:
            diff = restore.compute_diff(
                self.baselines.get(snap),
                self.connections_by_snap.get(snap) or {}, snap)
            steps.extend(diff["steps"])
            skipped.extend(diff["skipped"])
            not_restored.extend(diff["not_restored"])
        return steps, skipped, not_restored

    def confirm_restore(self, snaps):
        # snaps is None for all changed snaps, or a list of snap names.
        if self.busy or self.confirming or self.baseline_problem is not None:
            return
        if snaps is None:
            snaps = sorted(self.changed_snaps())
        snaps = [s for s in snaps if s in self.baselines]
        if not snaps:
            return
        steps, skipped, not_restored = self.restore_diff_for(snaps)
        if not steps and not not_restored:
            self.show_toast("Already matches the original state")
            return
        lines = ["Restore also reverts changes made outside Ginger since "
                 "the saved original state (for example with "
                 "snap connect)."]
        for step in steps:
            verb = "Connect" if step["action"] == "connect" \
                else "Disconnect"
            line = "%s %s:%s" % (verb, step["plug_snap"], step["plug"])
            if step["tier"] >= 2:
                line += " (sensitive interface)"
            lines.append(line)
        for item in not_restored:
            lines.append("Not restored by Ginger: %s (run ‘%s’)"
                         % (item["plug"], item["command"]))
        if skipped:
            lines.append("%d step(s) skipped: names no longer exist"
                         % len(skipped))
        lines.append("This will ask for approval %d times."
                     % len(steps))
        alert = Adw.AlertDialog(heading="Restore original connections?",
                                 body="\n".join(lines))
        alert.add_response("cancel", "Cancel")
        alert.add_response("restore", "Restore")
        alert.set_response_appearance(
            "restore", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.choose(self, None, self.on_restore_confirmed,
                     (snaps, steps))

    def on_restore_confirmed(self, source, result, data):
        if source.choose_finish(result) != "restore":
            return
        snaps, steps = data
        if not steps:
            return
        self.restore_batch = {"snaps": snaps, "steps": steps,
                               "index": 0}
        self.run_restore_step()

    def run_restore_step(self):
        batch = self.restore_batch
        if batch is None or self.closing:
            return
        if batch["index"] >= len(batch["steps"]):
            self.restore_batch = None
            self.show_toast("Restore finished")
            return
        step = batch["steps"][batch["index"]]
        n = len(batch["steps"])
        self.busy = True
        self.show_toast("Restoring: step %d of %d"
                        % (batch["index"] + 1, n))
        threading.Thread(
            target=self.change_worker,
            args=(step["action"], step["plug_snap"], step["plug"],
                  (step["slot_snap"], step["slot"])),
            daemon=True).start()

    def restore_step_done(self, outcome, message):
        batch = self.restore_batch
        if batch is None or self.closing:
            return
        done = batch["index"] + 1
        n = len(batch["steps"])
        if outcome == changes.OUTCOME_DONE:
            batch["index"] = done
            self.run_restore_step()
            return
        self.restore_batch = None
        if outcome == changes.OUTCOME_CANCELLED:
            self.show_toast("Stopped: %d of %d steps done" % (done, n))
        elif outcome == changes.OUTCOME_TIMEOUT:
            self.show_toast("State unknown, reloaded")
        else:
            self.show_toast("Stopped after %d of %d steps: %s"
                            % (done, n, message or "snapd error"))

    def permissions_group(self, name, connections):
        group = Adw.PreferencesGroup(
            title=GLib.markup_escape_text("Permissions"))
        plugs = interfaces.derive_plugs(connections, name,
                                         show_all=self.show_all_interfaces)
        if not plugs:
            group.add(Adw.ActionRow(title="No interfaces", use_markup=False))
            return group
        has_baseline = name in self.baselines
        for plug in plugs:
            group.add(self.plug_row(name, plug, has_baseline))
        return group

    def plug_row(self, snap_name, plug, has_baseline):
        row = Adw.SwitchRow(title=plug["name"],
                            subtitle=plug["interface"] + " \u00b7 "
                            + plug["state"],
                            use_markup=False)
        row.plug_info = plug
        row.plug_snap = snap_name
        row.plug_name = plug["name"]
        self.set_switch_active(row, plug["connected"])
        slot = None
        if plug["connected"]:
            slot = plug["conn_slot"]
        elif plug["tier"] == 3:
            row.set_subtitle(plug["interface"] + " \u00b7 " + plug["state"]
                            + " \u00b7 Connect not offered")
        elif plug["n_slots"] == 0:
            row.set_subtitle(plug["interface"] + " \u00b7 No slot available")
        elif plug["n_slots"] > 1:
            row.set_subtitle(plug["interface"]
                            + " \u00b7 Several slots available")
        else:
            slot = plug["slot"]
        row.plug_slot = slot
        if slot is None or plug["tier"] == 3 and not plug["connected"]:
            row.set_sensitive(False)
        elif not has_baseline or self.baseline_problem is not None:
            row.set_sensitive(False)
            if not has_baseline:
                row.set_tooltip_text(
                    "No saved original state for this snap yet; "
                    "restart Ginger to take one")
        row.connect("notify::active", self.on_switch_toggled)
        return row

    def set_switch_active(self, row, active):
        self.suppress_switch_handler = True
        try:
            row.set_active(active)
        finally:
            self.suppress_switch_handler = False

    def on_switch_toggled(self, row, pspec):
        if self.suppress_switch_handler:
            return
        plug = row.plug_info
        snap_name = row.plug_snap
        action = "connect" if row.get_active() else "disconnect"
        if changes.needs_confirmation(action, plug["interface"],
                                      plug["tier"]):
            self.confirm_change(row, snap_name, plug, action)
        else:
            self.start_change(row, snap_name, plug, action)

    def confirm_change(self, row, snap_name, plug, action):
        if self.confirming:
            self.set_switch_active(row, plug["connected"])
            return
        self.set_switch_active(row, not row.get_active())
        self.confirming = True
        body = changes.confirmation_body(action, plug["name"],
                                         plug["interface"], plug["tier"])
        if action == "connect":
            heading = "Connect %s?" % plug["name"]
            label = "Connect"
        else:
            heading = "Disconnect %s?" % plug["name"]
            label = "Disconnect"
        alert = Adw.AlertDialog(heading=heading, body=body)
        alert.add_response("cancel", "Cancel")
        alert.add_response("confirm", label)
        if action == "disconnect":
            alert.set_response_appearance(
                "confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.choose(self, None, self.on_change_confirmed,
                     (row, snap_name, plug, action))

    def on_change_confirmed(self, source, result, data):
        self.confirming = False
        row, snap_name, plug, action = data
        if source.choose_finish(result) != "confirm":
            return
        self.start_change(row, snap_name, plug, action)

    def start_change(self, row, snap_name, plug, action):
        if self.closing:
            return
        if self.busy or self.confirming:
            self.set_switch_active(row, plug["connected"])
            self.show_toast("Another change is running")
            return
        if snap_name not in self.baselines or self.baseline_problem \
                or row.plug_slot is None:
            self.set_switch_active(row, plug["connected"])
            self.show_toast("No saved original state for this snap yet")
            return
        self.busy = True
        self.set_switch_active(row, action == "connect")
        row.set_sensitive(False)
        spinner = Gtk.Spinner(spinning=True)
        row.add_suffix(spinner)
        row.change_spinner = spinner
        slot = row.plug_slot
        threading.Thread(
            target=self.change_worker,
            args=(action, snap_name, plug["name"], slot),
            daemon=True).start()

    def change_worker(self, action, snap_name, plug, slot):
        outcome, message = changes.run_change(
            self.client, action, snap_name, plug, slot[0], slot[1])
        GLib.idle_add(self.change_done, action, snap_name, plug,
                      slot, outcome, message)

    def change_done(self, action, snap_name, plug, slot, outcome, message):
        if self.closing:
            return False
        if self.restore_batch is not None:
            self.busy = False
            self.load()
            self.restore_step_done(outcome, message)
            return False
        self.busy = False
        self.load()
        if outcome == changes.OUTCOME_DONE:
            self.show_undo_toast(action, snap_name, plug, slot)
        elif outcome == changes.OUTCOME_CANCELLED:
            pass
        elif outcome == changes.OUTCOME_TIMEOUT:
            self.show_toast("State unknown, reloaded")
        else:
            alert = Adw.AlertDialog(heading="snapd returned an error",
                                    body=message or "")
            alert.add_response("ok", "OK")
            alert.present(self)
        return False

    def make_toast(self, title):
        toast = Adw.Toast(title=title)
        if hasattr(toast.props, "use_markup"):
            toast.set_use_markup(False)
        else:
            toast.set_title(GLib.markup_escape_text(title))
        return toast

    def show_toast(self, title):
        toast = self.make_toast(title)
        self.last_toast = toast
        self.toast_overlay.add_toast(toast)
        return toast

    def show_undo_toast(self, action, snap_name, plug, slot):
        inverse = "disconnect" if action == "connect" else "connect"
        verb = "Connected" if action == "connect" else "Disconnected"
        toast = self.show_toast("%s %s" % (verb, plug))
        toast.set_button_label("Undo")
        toast.connect("button-clicked", lambda t: self.undo_action(
            t, snap_name, plug, inverse, slot))

    def undo_action(self, toast, snap_name, plug, inverse, slot):
        toast.dismiss()
        row = self.find_plug_row(snap_name, plug)
        connections = self.connections_by_snap.get(snap_name) or {}
        plugs = interfaces.derive_plugs(connections, snap_name,
                                        show_all=True)
        info = next((p for p in plugs if p["name"] == plug), None)
        if row is None or info is None or slot is None \
                or (inverse == "connect" and info["tier"] == 3):
            self.last_toast = self.make_toast("Couldn't undo")
            self.toast_overlay.add_toast(self.last_toast)
            return
        row.plug_slot = slot
        self.set_switch_active(row, inverse == "connect")
        if changes.needs_confirmation(inverse, info["interface"],
                                      info["tier"]):
            self.confirm_change(row, snap_name, info, inverse)
        else:
            self.start_change(row, snap_name, info, inverse)

    def walk(self, widget):
        yield widget
        child = widget.get_first_child()
        while child:
            yield from self.walk(child)
            child = child.get_next_sibling()

    def find_plug_row(self, snap_name, plug):
        if snap_name != self.selected_snap:
            return None
        for row in self.walk(self.detail_bin.get_child()):
            if isinstance(row, Adw.SwitchRow) \
                    and getattr(row, "plug_name", None) == plug:
                return row
        return None

    def copy_text(self, button, text):
        # Gdk.Clipboard.set_text is not introspectable on some PyGObject
        # versions; a string content provider works everywhere.
        value = GObject.Value()
        value.init(GObject.TYPE_STRING)
        value.set_string(text)
        self.get_clipboard().set_content(
            Gdk.ContentProvider.new_for_value(value))

    def show_error(self, title, message, icon):
        self.error_page.set_title(title)
        self.error_page.set_description(GLib.markup_escape_text(message))
        self.error_page.set_icon_name(icon)
        self.page_stack.set_visible_child_name("error")

    def load(self):
        try:
            snaps = self.client.list_snaps()
            self.connections = self.client.list_connections()
        except SnapdError as e:
            if e.kind == "connection-failed":
                self.show_error("Could not reach snapd", e.message,
                                "network-error-symbolic")
            else:
                self.show_error("snapd returned an error", e.message,
                                "dialog-warning-symbolic")
            return
        self.baselines = {}
        baseline_problem = None
        used_backup = False
        try:
            self.baselines, used_backup = baseline.load()
        except baseline.BaselineError as e:
            baseline_problem = str(e)
        self.snaps = [s for s in snaps if isinstance(s, dict)]
        self.connections_by_snap = {}
        for name in self.snap_names():
            self.connections_by_snap[name] = self.connections
        if baseline_problem is None:
            new = baseline.capture_new(self.baselines,
                                       self.connections_by_snap)
            if new:
                try:
                    baseline.save_snaps({**self.baselines, **new})
                except OSError as e:
                    baseline_problem = \
                        "Could not save the original state: %s" % e
                else:
                    self.baselines.update(new)
        self.baseline_problem = baseline_problem
        if baseline_problem is not None:
            self.baseline_banner.set_title(
                "The saved original state cannot be read ("
                + baseline.baseline_path() + ")")
            self.baseline_banner.set_revealed(True)
        else:
            self.baseline_banner.set_revealed(False)
            if used_backup:
                self.show_toast("Using the backup copy of the saved "
                                "original state")
        if self.selected_snap not in self.snap_names():
            self.selected_snap = None
        if self.search_entry.get_text() != self.query:
            self.search_entry.set_text(self.query)
        self.page_stack.set_visible_child_name("snaps")
        self.refresh_list()
        if self.selected_snap is None:
            first = self.snaps_list.get_row_at_index(0)
            if first is not None and hasattr(first, "snap_name"):
                self.on_snap_selected(self.snaps_list, first,
                                      from_user=False)

    def setup_breakpoints(self):
        b1 = Adw.Breakpoint(condition=Adw.BreakpointCondition.parse(
            "max-width: 865"))
        b1.add_setter(self.main_split, "collapsed", True)
        b1.add_setter(self.main_split, "max-sidebar-width", 280)
        self.add_breakpoint(b1)
        b2 = Adw.Breakpoint(condition=Adw.BreakpointCondition.parse(
            "max-width: 600"))
        b2.add_setter(self.detail_split, "collapsed", True)
        b2.add_setter(self.main_split, "collapsed", True)
        b2.add_setter(self.main_split, "max-sidebar-width", 280)
        self.add_breakpoint(b2)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        register_icon_search_path()

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = Window(self)
        win.present()
        win.load()


def main():
    try:
        App().run(sys.argv)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
