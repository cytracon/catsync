from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Pango", "1.0")
from gi.repository import AyatanaAppIndicator3 as AppIndicator3  # noqa: E402
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from .config import Config, DEFAULT_IGNORE_PATTERNS, load_config, write_config
from .inventory import sync_path_allowed
from .state import State


APP_ID = "catsync-tray"
DOCK_WM_CLASS = "catsync-settings"
ICON_NAME = "catsyncd-tray"
ICON_BUSY_NAME = "catsyncd-tray-busy"
ICON_STOPPED_NAME = "catsyncd-tray-stopped"
PACKAGED_ICON_DIR = Path("/usr/share/icons/hicolor/scalable/apps")
PACKAGED_PIXMAP_DIR = Path("/usr/share/pixmaps")
USER_ICON_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "icons" / "hicolor" / "scalable" / "apps"
LOCAL_ICON_DIR = Path(__file__).resolve().parents[1] / "data" / "icons"
if not LOCAL_ICON_DIR.exists():
    LOCAL_ICON_DIR = Path(__file__).resolve().parents[1] / "assets"


class CatsyncTray:
    def __init__(self) -> None:
        self._setup_opened = False
        self.indicator = AppIndicator3.Indicator.new(
            APP_ID,
            _status_icon_ref(ICON_NAME),
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_icon_theme_path(str(_icon_dir()))
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Catsync")

        self.status_item = Gtk.MenuItem(label="Catsync: starting")
        self.status_item.set_sensitive(False)
        self.mode_item = Gtk.MenuItem(label="Mode: checking")
        self.mode_item.set_sensitive(False)

        menu = Gtk.Menu()
        menu.append(self.status_item)
        menu.append(self.mode_item)
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(_menu_item("Open Catsync Folder", self.open_folder))
        menu.append(_menu_item("Settings...", self.open_settings))
        menu.append(_menu_item("Activity...", self.open_activity))
        menu.append(_menu_item("Resolve Conflicts...", self.open_conflicts))
        menu.append(_menu_item("Run Plan Now", self.run_plan))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(_menu_item("Start Daemon", self.start_daemon))
        menu.append(_menu_item("Stop Daemon", self.stop_daemon))
        menu.append(Gtk.SeparatorMenuItem())
        menu.append(_menu_item("Quit Tray", self.quit))
        menu.show_all()

        self.indicator.set_menu(menu)
        self.update_status()
        GLib.timeout_add_seconds(15, self.update_status)
        GLib.timeout_add_seconds(2, self.maybe_open_setup)

    def update_status(self) -> bool:
        cfg = load_config()
        if not _oauth_client_available(cfg):
            self.status_item.set_label("Catsync: setup required - OAuth client missing")
            self.mode_item.set_label("Mode: open Settings > Account")
            self.indicator.set_icon_full(_status_icon_ref(ICON_STOPPED_NAME), "Catsync - setup required")
            self.indicator.set_title("Catsync - setup required")
            return True
        if not cfg.token_path.exists():
            self.status_item.set_label("Catsync: setup required - authorize Google Drive")
            self.mode_item.set_label("Mode: open Settings > Account")
            self.indicator.set_icon_full(_status_icon_ref(ICON_STOPPED_NAME), "Catsync - authorization required")
            self.indicator.set_title("Catsync - authorization required")
            return True
        summary, last_scan = _read_state(cfg.db_path)
        active = _service_active()
        phase, _updated = _daemon_phase(cfg.db_path)
        current = _current_activity(cfg.db_path)
        service = "running" if active else "stopped"
        plan = _format_summary(summary)
        scan = f", last scan {last_scan}" if last_scan else ""
        phase_text = f", {phase}" if active and phase not in {"unknown", "idle"} else ""
        activity_text = ""
        if current:
            activity_text = f", {_short_activity(current)}"
        self.status_item.set_label(f"Catsync: {service}{phase_text}{activity_text}{scan}")
        self.mode_item.set_label(_format_mode(cfg.dry_run, cfg.allow_writes, cfg.allow_deletes, plan))
        icon_name = _status_icon_name(active, phase)
        self.indicator.set_icon_full(_status_icon_ref(icon_name), f"Catsync - {service}{phase_text}")
        self.indicator.set_title(f"Catsync - {service}{phase_text}")
        return True

    def open_folder(self, _item: Gtk.MenuItem) -> None:
        cfg = load_config()
        cfg.sync_root.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["xdg-open", str(cfg.sync_root)])

    def run_plan(self, _item: Gtk.MenuItem) -> None:
        subprocess.Popen(_catsyncd_command("plan", "--summary-only"))
        self.status_item.set_label("Catsync: planning")

    def open_settings(self, _item: Gtk.MenuItem | None, initial_page: str | None = None) -> None:
        self._setup_opened = True
        dialog = SettingsDialog(initial_page=initial_page)
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            dialog.save()
            if _service_active():
                _service_action("restart", "catsyncd")
            self.update_status()
        dialog.destroy()

    def open_conflicts(self, _item: Gtk.MenuItem | None) -> None:
        dialog = ConflictDialog()
        dialog.run()
        dialog.destroy()
        self.update_status()

    def open_activity(self, _item: Gtk.MenuItem | None) -> None:
        dialog = ActivityDialog()
        dialog.run()
        dialog.destroy()
        self.update_status()

    def start_daemon(self, _item: Gtk.MenuItem) -> None:
        _service_action("start", "catsyncd")
        self.update_status()

    def stop_daemon(self, _item: Gtk.MenuItem) -> None:
        _service_action("stop", "catsyncd")
        self.update_status()

    def quit(self, _item: Gtk.MenuItem) -> None:
        Gtk.main_quit()

    def maybe_open_setup(self) -> bool:
        if self._setup_opened:
            return False
        cfg = load_config()
        if _oauth_client_available(cfg) and cfg.token_path.exists():
            return False
        self._setup_opened = True
        self.open_settings(None, initial_page="Account")
        return False


class SettingsDialog(Gtk.Dialog):
    def __init__(self, initial_page: str | None = None) -> None:
        super().__init__(title="Catsync Settings")
        _set_window_icon(self)
        self.set_default_size(760, 600)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        self.add_button("Save", Gtk.ResponseType.OK)
        self.cfg = load_config()
        self.page_numbers: dict[str, int] = {}

        box = self.get_content_area()
        self.notebook = Gtk.Notebook(margin=10)
        box.add(self.notebook)

        self._build_general_tab(self.notebook)
        self._build_account_tab(self.notebook)
        self._build_sync_tab(self.notebook)
        self._build_network_tab(self.notebook)
        self._build_ignore_tab(self.notebook)
        self._build_advanced_tab(self.notebook)

        if initial_page:
            self._select_page(initial_page)

        self.show_all()

    def _build_general_tab(self, notebook: Gtk.Notebook) -> None:
        grid = _grid()
        self.launch_daemon = _check("Start sync daemon at login", self.cfg.launch_daemon_at_login)
        self.launch_tray = _check("Show Catsync icon at login", self.cfg.launch_tray_at_login)
        self.notifications = _check("Show desktop notifications", self.cfg.desktop_notifications)
        self.poll_interval = _spin(self.cfg.poll_interval, 15, 3600, 15, 60)
        _attach_row(grid, 0, "Startup", self.launch_daemon)
        _attach_row(grid, 1, "Top bar", self.launch_tray)
        _attach_row(grid, 2, "Notifications", self.notifications)
        _attach_row(grid, 3, "Scan interval", self.poll_interval)
        self._append_page(notebook, grid, "General")

    def _build_account_tab(self, notebook: Gtk.Notebook) -> None:
        grid = _grid()
        self.client_secret_path = _readonly_entry(str(self.cfg.client_secret_path))
        self.token_path = _readonly_entry(str(self.cfg.token_path))
        self.account_status = Gtk.Label(label=_account_status_text(self.cfg), xalign=0)
        _attach_row(grid, 0, "OAuth client", self.client_secret_path)
        _attach_row(grid, 1, "Token", self.token_path)
        _attach_row(grid, 2, "Status", self.account_status)
        _attach_row(grid, 3, "Authorization", _button_row(("Authorize Google Drive", self._reauthorize), ("Refresh Folders", self._refresh_folder_list), ("Open Google Drive", self._open_drive)))
        self.setup_status = Gtk.Label(label=_setup_status_text(self.cfg), xalign=0)
        self.setup_status.set_line_wrap(True)
        _attach_row(grid, 4, "Setup", self.setup_status)
        self._append_page(notebook, grid, "Account")

    def _build_sync_tab(self, notebook: Gtk.Notebook) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=16)
        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        outer.pack_start(grid, False, False, 0)
        self.sync_root = Gtk.FileChooserButton(title="Choose Base Folder", action=Gtk.FileChooserAction.SELECT_FOLDER)
        self.sync_root.set_filename(str(self.cfg.sync_root))
        self.folder_store = Gtk.TreeStore(bool, str, str)
        self._populate_folder_store(_local_folder_paths(self.cfg))
        self.folder_status = Gtk.Label(label=_folder_status_text(self.cfg), xalign=0)
        self.folder_tree = Gtk.TreeView(model=self.folder_store)
        self.folder_tree.set_headers_visible(False)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._toggle_folder)
        folder_column = Gtk.TreeViewColumn("Sync", toggle, active=0)
        self.folder_tree.append_column(folder_column)
        text = Gtk.CellRendererText()
        self.folder_tree.append_column(Gtk.TreeViewColumn("Folder", text, text=1))
        selective_scroller = Gtk.ScrolledWindow()
        selective_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        selective_scroller.set_min_content_height(190)
        selective_scroller.add(self.folder_tree)
        sync_all = not self.cfg.selective_sync_paths and not self.cfg.excluded_sync_paths
        self.sync_all = _check("Sync all local folders", sync_all)
        self.sync_all.connect("toggled", self._toggle_sync_all)
        self.shared_drives = _check("Include shared drives", self.cfg.include_shared_drives)
        self.sync_symlinks = _check("Sync symlinks", self.cfg.sync_symlinks)
        self.dry_run = _check("Dry-run mode", self.cfg.dry_run)
        self.allow_writes = _check("Allow uploads, downloads, and moves", self.cfg.allow_writes)
        self.allow_deletes = _check("Allow delete propagation to trash", self.cfg.allow_deletes)
        self.conflict_policy = _combo(
            (
                ("prefer_newer", "Prefer newer when clear"),
                ("keep_both", "Keep both copies"),
                ("review", "Review conflicts"),
                ("prefer_local", "Prefer local"),
                ("prefer_remote", "Prefer remote"),
            ),
            self.cfg.conflict_policy,
        )
        self.google_docs_mode = _combo(
            (("placeholder", "Create local Google-file placeholders"), ("skip", "Skip Google Docs/Sheets/Slides")),
            self.cfg.google_docs_mode,
        )
        _attach_row(grid, 0, "Base folder", self.sync_root)
        _attach_row(grid, 1, "Shared drives", self.shared_drives)
        _attach_row(grid, 2, "Symlinks", self.sync_symlinks)
        outer.pack_start(self.sync_all, False, False, 0)
        outer.pack_start(_button_row(("Refresh Folder List", self._refresh_folder_list)), False, False, 0)
        outer.pack_start(self.folder_status, False, False, 0)
        outer.pack_start(Gtk.Label(label="Selective sync: uncheck folders you do not want on this computer.", xalign=0), False, False, 0)
        outer.pack_start(selective_scroller, True, True, 0)
        grid2 = Gtk.Grid(column_spacing=12, row_spacing=10)
        outer.pack_start(grid2, False, False, 0)
        _attach_row(grid2, 0, "Safety", self.dry_run)
        _attach_row(grid2, 1, "Writes", self.allow_writes)
        _attach_row(grid2, 2, "Deletes", self.allow_deletes)
        _attach_row(grid2, 3, "Conflicts", self.conflict_policy)
        _attach_row(grid2, 4, "Google files", self.google_docs_mode)
        self._append_page(notebook, outer, "Sync")

    def _build_network_tab(self, notebook: Gtk.Notebook) -> None:
        grid = _grid()
        self.proxy_mode = _combo((("system", "Use system proxy"), ("none", "No proxy"), ("manual", "Manual proxy")), self.cfg.proxy_mode)
        self.proxy_host = Gtk.Entry()
        self.proxy_host.set_text(self.cfg.proxy_host)
        self.proxy_port = _spin(self.cfg.proxy_port, 0, 65535, 1, 100)
        self.download_limit = _spin(self.cfg.download_limit_kbps, 0, 1000000, 100, 1000)
        self.upload_limit = _spin(self.cfg.upload_limit_kbps, 0, 1000000, 100, 1000)
        _attach_row(grid, 0, "Proxy", self.proxy_mode)
        _attach_row(grid, 1, "Proxy host", self.proxy_host)
        _attach_row(grid, 2, "Proxy port", self.proxy_port)
        _attach_row(grid, 3, "Download limit KB/s", self.download_limit)
        _attach_row(grid, 4, "Upload limit KB/s", self.upload_limit)
        self._append_page(notebook, grid, "Network")

    def _build_ignore_tab(self, notebook: Gtk.Notebook) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=16)
        self.ignore_patterns = Gtk.TextView()
        self.ignore_patterns.set_monospace(True)
        self.ignore_patterns.get_buffer().set_text("\n".join(self.cfg.ignore_patterns))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.ignore_patterns)
        outer.pack_start(Gtk.Label(label="Global excludes: one pattern per line. Folder-name patterns apply anywhere in the tree.", xalign=0), False, False, 0)
        outer.pack_start(_button_row(("Restore Defaults", self._restore_default_ignores)), False, False, 0)
        outer.pack_start(scroller, True, True, 0)
        self._append_page(notebook, outer, "Ignore Rules")

    def _build_advanced_tab(self, notebook: Gtk.Notebook) -> None:
        grid = _grid()
        self.hash_limit_mb = _spin(max(1, self.cfg.hash_limit_bytes // (1024 * 1024)), 1, 10240, 1, 64)
        self.daemon_max_actions = _spin(self.cfg.daemon_max_actions, 1, 10000, 1, 100)
        self.db_path = _readonly_entry(str(self.cfg.db_path))
        _attach_row(grid, 0, "Hash limit MB", self.hash_limit_mb)
        _attach_row(grid, 1, "Max actions per cycle", self.daemon_max_actions)
        _attach_row(grid, 2, "State database", self.db_path)
        _attach_row(grid, 3, "Maintenance", _button_row(("Run Plan", self._run_plan), ("Open Config Folder", self._open_config_folder)))
        self._append_page(notebook, grid, "Advanced")

    def _append_page(self, notebook: Gtk.Notebook, child: Gtk.Widget, title: str) -> None:
        page = notebook.append_page(child, Gtk.Label(label=title))
        self.page_numbers[title] = page

    def _select_page(self, title: str) -> None:
        page = self.page_numbers.get(title)
        if page is not None:
            self.notebook.set_current_page(page)

    def save(self) -> None:
        sync_root = Path(self.sync_root.get_filename() or str(self.cfg.sync_root)).expanduser().resolve()
        cfg = Config(
            sync_root=sync_root,
            client_secret_path=self.cfg.client_secret_path,
            token_path=self.cfg.token_path,
            db_path=self.cfg.db_path,
            poll_interval=int(self.poll_interval.get_value_as_int()),
            allow_writes=bool(self.allow_writes.get_active()),
            allow_deletes=bool(self.allow_deletes.get_active()),
            dry_run=bool(self.dry_run.get_active()),
            include_shared_drives=bool(self.shared_drives.get_active()),
            hash_limit_bytes=int(self.hash_limit_mb.get_value_as_int()) * 1024 * 1024,
            launch_daemon_at_login=bool(self.launch_daemon.get_active()),
            launch_tray_at_login=bool(self.launch_tray.get_active()),
            desktop_notifications=bool(self.notifications.get_active()),
            conflict_policy=_combo_value(self.conflict_policy),
            google_docs_mode=_combo_value(self.google_docs_mode),
            proxy_mode=_combo_value(self.proxy_mode),
            proxy_host=self.proxy_host.get_text().strip(),
            proxy_port=int(self.proxy_port.get_value_as_int()),
            upload_limit_kbps=int(self.upload_limit.get_value_as_int()),
            download_limit_kbps=int(self.download_limit.get_value_as_int()),
            ignore_patterns=_text_lines(self.ignore_patterns),
            selective_sync_paths=(),
            excluded_sync_paths=() if self.sync_all.get_active() else _unchecked_folder_paths(self.folder_store),
            sync_symlinks=bool(self.sync_symlinks.get_active()),
            daemon_max_actions=int(self.daemon_max_actions.get_value_as_int()),
        )
        write_config(cfg)
        _set_service_enabled("catsyncd.service", cfg.launch_daemon_at_login)
        _set_service_enabled("catsync-tray.service", cfg.launch_tray_at_login)

    def _reauthorize(self, _button: Gtk.Button) -> None:
        self.account_status.set_text("Authorization started in browser...")
        self.setup_status.set_text("Waiting for Google authorization in the browser...")
        threading.Thread(target=self._authorize_worker, daemon=True).start()

    def _open_drive(self, _button: Gtk.Button) -> None:
        subprocess.Popen(["xdg-open", "https://drive.google.com/drive/my-drive"])

    def _run_plan(self, _button: Gtk.Button) -> None:
        subprocess.Popen(_catsyncd_command("plan", "--summary-only"))

    def _refresh_folder_list(self, _button: Gtk.Button) -> None:
        self.folder_status.set_text("Refreshing local folders...")
        threading.Thread(target=self._refresh_folder_worker, daemon=True).start()

    def _open_config_folder(self, _button: Gtk.Button) -> None:
        subprocess.Popen(["xdg-open", str(self.cfg.client_secret_path.parent)])

    def _restore_default_ignores(self, _button: Gtk.Button) -> None:
        existing = set(_text_lines(self.ignore_patterns))
        patterns = tuple(sorted(existing | set(DEFAULT_IGNORE_PATTERNS)))
        self.ignore_patterns.get_buffer().set_text("\n".join(patterns))

    def _authorize_worker(self) -> None:
        result = subprocess.run(_catsyncd_command("auth"), check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0:
            subprocess.run(_catsyncd_command("plan", "--summary-only"), check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _service_action("restart", "catsyncd")
        GLib.idle_add(self._finish_background_setup, result.returncode, result.stderr.strip() or result.stdout.strip())

    def _refresh_folder_worker(self) -> None:
        result = subprocess.run(_catsyncd_command("plan", "--summary-only"), check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        GLib.idle_add(self._finish_background_setup, result.returncode, result.stderr.strip() or result.stdout.strip())

    def _finish_background_setup(self, returncode: int, detail: str) -> bool:
        self.cfg = load_config()
        if returncode == 0:
            self.account_status.set_text(_account_status_text(self.cfg))
            self.setup_status.set_text(_setup_status_text(self.cfg))
            self.folder_store.clear()
            self._populate_folder_store(_local_folder_paths(self.cfg))
            self.folder_status.set_text(_folder_status_text(self.cfg))
            if self.cfg.token_path.exists():
                self._select_page("Sync")
        else:
            message = detail or "failed"
            self.account_status.set_text(f"Setup failed: {message}")
            self.setup_status.set_text(f"Setup failed: {message}")
            self.folder_status.set_text(f"Refresh failed: {message}")
        return False

    def _populate_folder_store(self, paths: list[str]) -> None:
        nodes: dict[str, Gtk.TreeIter] = {}
        for folder_path in paths:
            parts = folder_path.split("/")
            parent = None
            built = []
            for part in parts:
                built.append(part)
                path = "/".join(built)
                if path in nodes:
                    parent = nodes[path]
                    continue
                checked = self._folder_checked(path)
                node = self.folder_store.append(parent, [checked, part, path])
                nodes[path] = node
                parent = node

    def _folder_checked(self, path: str) -> bool:
        selected = tuple(self.cfg.selective_sync_paths)
        excluded = tuple(self.cfg.excluded_sync_paths)
        if selected and not _path_selected(path, selected):
            return False
        return not _path_excluded(path, excluded)

    def _toggle_folder(self, _cell: Gtk.CellRendererToggle, tree_path: str) -> None:
        iterator = self.folder_store.get_iter(tree_path)
        checked = not bool(self.folder_store.get_value(iterator, 0))
        self.sync_all.set_active(False)
        _set_tree_checked(self.folder_store, iterator, checked)
        if checked:
            _set_ancestors_checked(self.folder_store, iterator)

    def _toggle_sync_all(self, button: Gtk.CheckButton) -> None:
        if button.get_active():
            iterator = self.folder_store.get_iter_first()
            while iterator is not None:
                _set_tree_checked(self.folder_store, iterator, True)
                iterator = self.folder_store.iter_next(iterator)


class ConflictDialog(Gtk.Dialog):
    def __init__(self) -> None:
        super().__init__(title="Catsync Conflicts")
        _set_window_icon(self)
        self.set_default_size(1100, 720)
        self.set_size_request(820, 560)
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.cfg = load_config()
        self.store = Gtk.ListStore(str, str, str, str, str, str, str, str, str)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.get_content_area().add(outer)

        self.daemon_status = Gtk.Label(xalign=0)
        outer.pack_start(self.daemon_status, False, False, 0)

        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_headers_visible(True)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        for title, column, width in (
            ("Action", 0, 150),
            ("Path", 1, 360),
            ("Newer", 2, 90),
            ("Local Modified", 3, 145),
            ("Remote Modified", 4, 145),
            ("Local Size", 5, 95),
            ("Remote Size", 6, 95),
            ("Reason", 7, 210),
            ("Status", 8, 110),
        ):
            text = Gtk.CellRendererText()
            text.set_property("ellipsize", Pango.EllipsizeMode.END)
            view_column = Gtk.TreeViewColumn(title, text, text=column)
            view_column.set_resizable(True)
            view_column.set_min_width(width)
            self.tree.append_column(view_column)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(420)
        scroller.add(self.tree)
        outer.pack_start(scroller, True, True, 0)

        action_scroller = Gtk.ScrolledWindow()
        action_scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        action_scroller.set_shadow_type(Gtk.ShadowType.NONE)
        action_scroller.add(self._build_action_row())
        outer.pack_start(action_scroller, False, False, 0)
        self._populate()
        self._refresh_status()
        GLib.timeout_add_seconds(5, self._refresh_status)
        self.show_all()

    def _build_action_row(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for label, callback in (
            ("Refresh", self._refresh),
            ("Select All", self._select_all),
            ("Select Resolvable", self._select_resolvable),
            ("Open Local", self._open_local),
            ("Open Parent", self._open_parent),
            ("Copy Path", self._copy_path),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", callback)
            box.pack_start(button, False, False, 0)

        self.resolve_buttons: list[Gtk.Button] = []
        for label, callback in (
            ("Keep Both", self._keep_both),
            ("Prefer Newer", self._prefer_newer),
            ("Prefer Local", self._prefer_local),
            ("Prefer Remote", self._prefer_remote),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", callback)
            self.resolve_buttons.append(button)
            box.pack_start(button, False, False, 0)
        return box

    def _populate(self) -> None:
        self.store.clear()
        state = State(self.cfg.db_path)
        try:
            actions = state.conflict_actions()
        finally:
            state.close()
        for action in actions:
            status = "manual" if action.action == "conflict_review" else "resolvable"
            local_mtime = action.local.mtime_ns if action.local else None
            remote_mtime = action.remote.modified_time if action.remote else None
            self.store.append(
                [
                    action.action,
                    action.path,
                    _newer_side(local_mtime, remote_mtime),
                    _format_local_mtime(local_mtime),
                    _format_remote_mtime(remote_mtime),
                    _format_size(action.local.size if action.local else None),
                    _format_size(action.remote.size if action.remote else None),
                    action.reason,
                    status,
                ]
            )

    def _selected_path(self) -> str | None:
        paths = self._selected_paths()
        if not paths:
            _message(self, "No conflict selected")
            return None
        return paths[0]

    def _selected_paths(self) -> list[str]:
        model, tree_paths = self.tree.get_selection().get_selected_rows()
        paths: list[str] = []
        for tree_path in tree_paths:
            iterator = model.get_iter(tree_path)
            paths.append(str(model.get_value(iterator, 1)))
        return paths

    def _refresh(self, _button: Gtk.Button) -> None:
        self._populate()
        self._refresh_status()

    def _select_all(self, _button: Gtk.Button) -> None:
        self.tree.get_selection().select_all()

    def _select_resolvable(self, _button: Gtk.Button) -> None:
        selection = self.tree.get_selection()
        selection.unselect_all()
        iterator = self.store.get_iter_first()
        while iterator is not None:
            if str(self.store.get_value(iterator, 8)) == "resolvable":
                selection.select_iter(iterator)
            iterator = self.store.iter_next(iterator)

    def _refresh_status(self) -> bool:
        phase, updated = _daemon_phase(self.cfg.db_path)
        ready = phase in {"idle", "stopped", "unknown"} or not _service_active()
        state = "idle - resolving enabled" if ready else f"busy ({phase}) - resolving disabled"
        suffix = f", updated {updated}" if updated else ""
        self.daemon_status.set_text(f"Daemon: {state}{suffix}")
        for button in getattr(self, "resolve_buttons", []):
            button.set_sensitive(ready)
        if ready:
            self._populate()
        return bool(self.get_visible())

    def _open_local(self, _button: Gtk.Button) -> None:
        path = self._selected_path()
        if not path:
            return
        subprocess.Popen(["xdg-open", str(self.cfg.sync_root / path)])

    def _open_parent(self, _button: Gtk.Button) -> None:
        path = self._selected_path()
        if not path:
            return
        subprocess.Popen(["xdg-open", str((self.cfg.sync_root / path).parent)])

    def _copy_path(self, _button: Gtk.Button) -> None:
        paths = self._selected_paths()
        if not paths:
            _message(self, "No conflict selected")
            return
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("\n".join(paths), -1)

    def _keep_both(self, _button: Gtk.Button) -> None:
        self._resolve("keep-both")

    def _prefer_newer(self, _button: Gtk.Button) -> None:
        self._resolve("prefer-newer")

    def _prefer_local(self, _button: Gtk.Button) -> None:
        self._resolve("prefer-local")

    def _prefer_remote(self, _button: Gtk.Button) -> None:
        self._resolve("prefer-remote")

    def _resolve(self, strategy: str) -> None:
        paths = self._selected_paths()
        if not paths:
            _message(self, "No conflict selected")
            return
        phase, _updated = _daemon_phase(self.cfg.db_path)
        if phase not in {"idle", "stopped", "unknown"} and _service_active():
            _message(self, f"Catsync is busy ({phase}); resolving will be enabled when it is idle.")
            self._refresh_status()
            return
        resolved = 0
        failed: list[str] = []
        for path in paths:
            result = subprocess.run(
                _catsyncd_command("resolve-conflict", path, "--strategy", strategy),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode == 0:
                resolved += 1
            else:
                detail = result.stderr.strip() or result.stdout.strip() or "failed"
                failed.append(f"{path}: {detail}")
        self._populate()
        if failed:
            sample = "\n".join(failed[:8])
            more = f"\n... {len(failed) - 8} more" if len(failed) > 8 else ""
            _message(self, f"Resolved {resolved}. Failed {len(failed)}:\n{sample}{more}")
        else:
            _message(self, f"Resolved {resolved} conflict(s)")


class ActivityDialog(Gtk.Dialog):
    def __init__(self) -> None:
        super().__init__(title="Catsync Activity")
        _set_window_icon(self)
        self.set_default_size(1100, 650)
        self.set_size_request(820, 520)
        self.add_button("Close", Gtk.ResponseType.CLOSE)
        self.cfg = load_config()
        self.store = Gtk.ListStore(str, str, str, str, str)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        self.get_content_area().add(outer)

        self.daemon_status = Gtk.Label(xalign=0)
        outer.pack_start(self.daemon_status, False, False, 0)

        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_headers_visible(True)
        self.tree.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        for title, column, width in (
            ("Status", 0, 120),
            ("Action", 1, 150),
            ("Path", 2, 460),
            ("Detail", 3, 250),
            ("Updated", 4, 150),
        ):
            text = Gtk.CellRendererText()
            text.set_property("ellipsize", Pango.EllipsizeMode.END)
            view_column = Gtk.TreeViewColumn(title, text, text=column)
            view_column.set_resizable(True)
            view_column.set_min_width(width)
            self.tree.append_column(view_column)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(430)
        scroller.add(self.tree)
        outer.pack_start(scroller, True, True, 0)

        outer.pack_start(self._build_action_row(), False, False, 0)
        self._populate()
        GLib.timeout_add_seconds(3, self._refresh)
        self.show_all()

    def _build_action_row(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for label, callback in (
            ("Refresh", self._refresh_button),
            ("Open Local", self._open_local),
            ("Open Parent", self._open_parent),
            ("Copy Path", self._copy_path),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", callback)
            box.pack_start(button, False, False, 0)
        return box

    def _populate(self) -> None:
        self.store.clear()
        status = _read_status_file(self.cfg.db_path)
        phase = str(status.get("phase") or "unknown")
        updated = _format_status_time(status.get("updated_at"))
        self.daemon_status.set_text(f"Daemon: {phase}, updated {updated}" if updated else f"Daemon: {phase}")

        activity = status.get("activity") if isinstance(status.get("activity"), dict) else {}
        current = activity.get("current") if isinstance(activity, dict) else None
        if isinstance(current, dict):
            self._append_operation("running", current)

        recent = activity.get("recent") if isinstance(activity, dict) else None
        if isinstance(recent, list):
            for operation in recent:
                if isinstance(operation, dict):
                    self._append_operation(str(operation.get("stage") or "done"), operation)

        for action in _visible_plan_actions(self.cfg):
            detail = action.reason
            if action.local and action.local.size is not None:
                detail = f"{detail}; local {_format_size(action.local.size)}"
            elif action.remote and action.remote.size is not None:
                detail = f"{detail}; remote {_format_size(action.remote.size)}"
            self.store.append(["planned", action.action, action.path, detail, ""])

    def _append_operation(self, status: str, operation: dict) -> None:
        count = operation.get("executed_count")
        total = operation.get("total_executable")
        detail_parts = [str(operation.get("label") or "")]
        if operation.get("size") is not None:
            detail_parts.append(_format_size(int(operation["size"])))
        if isinstance(count, int) and isinstance(total, int) and total:
            detail_parts.append(f"{count}/{total}")
        if operation.get("error"):
            detail_parts.append(str(operation["error"]))
        self.store.append(
            [
                status,
                str(operation.get("action") or ""),
                str(operation.get("path") or ""),
                "; ".join(part for part in detail_parts if part),
                _format_status_time(operation.get("updated_at")),
            ]
        )

    def _refresh_button(self, _button: Gtk.Button) -> None:
        self._populate()

    def _refresh(self) -> bool:
        self._populate()
        return bool(self.get_visible())

    def _selected_paths(self) -> list[str]:
        model, tree_paths = self.tree.get_selection().get_selected_rows()
        paths: list[str] = []
        for tree_path in tree_paths:
            iterator = model.get_iter(tree_path)
            path = str(model.get_value(iterator, 2))
            if path:
                paths.append(path)
        return paths

    def _selected_path(self) -> str | None:
        paths = self._selected_paths()
        if not paths:
            _message(self, "No activity selected")
            return None
        return paths[0]

    def _open_local(self, _button: Gtk.Button) -> None:
        path = self._selected_path()
        if path:
            subprocess.Popen(["xdg-open", str(self.cfg.sync_root / path)])

    def _open_parent(self, _button: Gtk.Button) -> None:
        path = self._selected_path()
        if path:
            subprocess.Popen(["xdg-open", str((self.cfg.sync_root / path).parent)])

    def _copy_path(self, _button: Gtk.Button) -> None:
        paths = self._selected_paths()
        if not paths:
            _message(self, "No activity selected")
            return
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text("\n".join(paths), -1)


def main() -> int:
    _set_process_identity()
    ok, _args = Gtk.init_check(sys.argv)
    if not ok:
        print("Catsync tray: no GTK display connection available; exiting without starting tray.", file=sys.stderr)
        return 75
    _set_default_window_icon()
    if "--settings" in sys.argv[1:] or "--setup" in sys.argv[1:]:
        return _run_settings_standalone(initial_page="Account" if "--setup" in sys.argv[1:] else None)
    tray = CatsyncTray()
    Gtk.main()
    return 0


def _run_settings_standalone(initial_page: str | None = None) -> int:
    dialog = SettingsDialog(initial_page=initial_page)
    response = dialog.run()
    if response == Gtk.ResponseType.OK:
        dialog.save()
        if _service_active():
            _service_action("restart", "catsyncd")
    dialog.destroy()
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)
    return 0


def _menu_item(label: str, callback) -> Gtk.MenuItem:
    item = Gtk.MenuItem(label=label)
    item.connect("activate", callback)
    return item


def _grid() -> Gtk.Grid:
    return Gtk.Grid(column_spacing=12, row_spacing=10, margin=16)


def _attach_row(grid: Gtk.Grid, row: int, label: str, widget: Gtk.Widget) -> None:
    grid.attach(_label(label), 0, row, 1, 1)
    grid.attach(widget, 1, row, 1, 1)


def _label(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.set_size_request(150, -1)
    return label


def _check(label: str, active: bool) -> Gtk.CheckButton:
    check = Gtk.CheckButton(label=label)
    check.set_active(active)
    return check


def _spin(value: int, lower: int, upper: int, step: int, page: int) -> Gtk.SpinButton:
    spin = Gtk.SpinButton()
    spin.set_range(lower, upper)
    spin.set_increments(step, page)
    spin.set_value(value)
    return spin


def _combo(items: tuple[tuple[str, str], ...], active_value: str) -> Gtk.ComboBoxText:
    combo = Gtk.ComboBoxText()
    for value, label in items:
        combo.append(value, label)
    combo.set_active_id(active_value)
    if combo.get_active_id() is None and items:
        combo.set_active(0)
    return combo


def _combo_value(combo: Gtk.ComboBoxText) -> str:
    return combo.get_active_id() or ""


def _readonly_entry(text: str) -> Gtk.Entry:
    entry = Gtk.Entry()
    entry.set_text(text)
    entry.set_editable(False)
    return entry


def _button_row(*buttons: tuple[str, object]) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    for label, callback in buttons:
        button = Gtk.Button(label=label)
        button.connect("clicked", callback)
        box.pack_start(button, False, False, 0)
    return box


def _message(parent: Gtk.Window, text: str) -> None:
    dialog = Gtk.MessageDialog(
        transient_for=parent,
        flags=Gtk.DialogFlags.MODAL,
        message_type=Gtk.MessageType.INFO,
        buttons=Gtk.ButtonsType.OK,
        text=text,
    )
    dialog.run()
    dialog.destroy()


def _format_local_mtime(value: int | None) -> str:
    if value is None:
        return "-"
    try:
        return datetime.fromtimestamp(value / 1_000_000_000).astimezone().strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "-"


def _format_remote_mtime(value: str | None) -> str:
    parsed = _parse_remote_mtime(value)
    if parsed is None:
        return "-"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def _newer_side(local_mtime_ns: int | None, remote_mtime: str | None) -> str:
    if local_mtime_ns is None or remote_mtime is None:
        return "-"
    remote = _parse_remote_mtime(remote_mtime)
    if remote is None:
        return "-"
    try:
        local = datetime.fromtimestamp(local_mtime_ns / 1_000_000_000, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return "-"
    delta = local.timestamp() - remote.timestamp()
    if abs(delta) < 1:
        return "same"
    return "local" if delta > 0 else "remote"


def _parse_remote_mtime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_size(value: int | None) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return str(value)


def _text_lines(view: Gtk.TextView) -> tuple[str, ...]:
    buffer = view.get_buffer()
    text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
    return tuple(line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#"))


def _local_folder_paths(cfg: Config) -> list[str]:
    root = cfg.sync_root.expanduser().resolve()
    if not root.exists():
        return []
    paths: list[str] = []
    for current, dirnames, _filenames in os.walk(root):
        current_path = Path(current)
        dirnames.sort()
        kept: list[str] = []
        for dirname in dirnames:
            path = current_path / dirname
            try:
                if path.is_symlink():
                    continue
                rel = path.relative_to(root).as_posix()
            except OSError:
                continue
            if sync_path_allowed(rel, cfg.ignore_patterns, (), ()):
                paths.append(rel)
                kept.append(dirname)
        dirnames[:] = kept
    return paths


def _set_tree_checked(store: Gtk.TreeStore, iterator: Gtk.TreeIter, checked: bool) -> None:
    store.set_value(iterator, 0, checked)
    child = store.iter_children(iterator)
    while child is not None:
        _set_tree_checked(store, child, checked)
        child = store.iter_next(child)


def _set_ancestors_checked(store: Gtk.TreeStore, iterator: Gtk.TreeIter) -> None:
    parent = store.iter_parent(iterator)
    while parent is not None:
        store.set_value(parent, 0, True)
        parent = store.iter_parent(parent)


def _unchecked_folder_paths(store: Gtk.TreeStore) -> tuple[str, ...]:
    paths: list[str] = []

    def walk(iterator: Gtk.TreeIter) -> None:
        checked = bool(store.get_value(iterator, 0))
        path = str(store.get_value(iterator, 2))
        if not checked:
            paths.append(path)
            return
        child = store.iter_children(iterator)
        while child is not None:
            walk(child)
            child = store.iter_next(child)

    iterator = store.get_iter_first()
    while iterator is not None:
        walk(iterator)
        iterator = store.iter_next(iterator)
    return tuple(paths)


def _path_selected(path: str, selected_paths: tuple[str, ...]) -> bool:
    if not selected_paths:
        return True
    path = path.strip("/")
    return any(path == selected or path.startswith(f"{selected}/") or selected.startswith(f"{path}/") for selected in selected_paths)


def _path_excluded(path: str, excluded_paths: tuple[str, ...]) -> bool:
    path = path.strip("/")
    return any(path == excluded or path.startswith(f"{excluded}/") for excluded in excluded_paths)


def _set_service_enabled(service: str, enabled: bool) -> None:
    command = "enable" if enabled else "disable"
    subprocess.run(["systemctl", "--user", command, service], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _service_action(action: str, service: str) -> None:
    subprocess.Popen(["systemctl", "--user", "--no-block", action, service], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _catsyncd_command(*args: str) -> list[str]:
    local = Path.home() / ".local" / "bin" / "catsyncd"
    if local.exists():
        return [str(local), *args]
    found = shutil.which("catsyncd")
    if found:
        return [found, *args]
    return [sys.executable, "-m", "catsyncd", *args]


def _icon_dir() -> Path:
    if (USER_ICON_DIR / f"{ICON_NAME}.svg").exists():
        return USER_ICON_DIR
    if (PACKAGED_PIXMAP_DIR / f"{ICON_NAME}.png").exists():
        return PACKAGED_PIXMAP_DIR
    if (PACKAGED_ICON_DIR / f"{ICON_NAME}.svg").exists():
        return PACKAGED_ICON_DIR
    return LOCAL_ICON_DIR


def _status_icon_ref(name: str) -> str:
    pixmap = PACKAGED_PIXMAP_DIR / f"{name}.png"
    if pixmap.exists():
        return str(pixmap)
    for directory in (USER_ICON_DIR, PACKAGED_ICON_DIR, LOCAL_ICON_DIR):
        path = directory / f"{name}.svg"
        if path.exists():
            return str(path)
    return name


def _app_icon_ref() -> Path | None:
    for path in (
        USER_ICON_DIR / "catsync.svg",
        USER_ICON_DIR / f"{ICON_NAME}.svg",
        PACKAGED_PIXMAP_DIR / "catsync.png",
        PACKAGED_PIXMAP_DIR / f"{ICON_NAME}.png",
        PACKAGED_ICON_DIR / "catsync.svg",
        PACKAGED_ICON_DIR / f"{ICON_NAME}.svg",
        LOCAL_ICON_DIR / "catsync.svg",
        LOCAL_ICON_DIR / f"{ICON_NAME}.svg",
    ):
        if path.exists():
            return path
    return None


def _set_default_window_icon() -> None:
    path = _app_icon_ref()
    if path is None:
        return
    try:
        Gtk.Window.set_default_icon_from_file(str(path))
    except GLib.Error:
        pass


def _set_window_icon(window: Gtk.Window) -> None:
    try:
        window.set_wmclass(DOCK_WM_CLASS, DOCK_WM_CLASS)
        window.set_role(DOCK_WM_CLASS)
    except (AttributeError, TypeError):
        pass
    path = _app_icon_ref()
    if path is None:
        return
    try:
        window.set_icon_from_file(str(path))
    except GLib.Error:
        pass


def _set_process_identity() -> None:
    GLib.set_prgname(DOCK_WM_CLASS)
    GLib.set_application_name("Catsync")
    try:
        Gdk.set_program_class(DOCK_WM_CLASS)
    except (AttributeError, TypeError):
        pass


def _status_icon_name(active: bool, phase: str) -> str:
    if not active:
        return ICON_STOPPED_NAME
    if phase in {"idle", "stopped", "unknown"}:
        return ICON_NAME
    return ICON_BUSY_NAME


def _service_active() -> bool:
    return subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "catsyncd"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def _read_state(db_path: Path) -> tuple[dict[str, int], str | None]:
    state = State(db_path)
    try:
        summary = state.plan_summary()
        last_scan = state.get_meta("last_scan")
        if last_scan:
            try:
                last_scan = _format_epoch(int(last_scan))
            except ValueError:
                pass
        return summary, last_scan
    finally:
        state.close()


def _visible_plan_actions(cfg: Config):
    actions = (
        "download_create",
        "download_update",
        "upload_create",
        "upload_update",
        "local_move",
        "remote_move",
        "local_delete",
        "remote_trash",
        "conflict_copy_local",
        "conflict_review",
    )
    state = State(cfg.db_path)
    try:
        return state.list_plan_actions(actions, limit=500)
    finally:
        state.close()


def _format_summary(summary: dict[str, int]) -> str:
    if not summary:
        return "no plan"
    return ", ".join(f"{name}: {count}" for name, count in sorted(summary.items()))


def _format_mode(dry_run: bool, allow_writes: bool, allow_deletes: bool, plan: str) -> str:
    mode = "dry-run" if dry_run or not allow_writes else "writes enabled"
    deletes = ", deletes enabled" if allow_deletes else ""
    return f"Mode: {mode}{deletes}; {plan}"


def _account_status_text(cfg: Config) -> str:
    if not _oauth_client_available(cfg):
        return "OAuth client missing"
    if not cfg.token_path.exists():
        return "Google Drive authorization required"
    return "Google Drive authorized"


def _oauth_client_available(cfg: Config) -> bool:
    return cfg.client_secret_path.exists()


def _folder_status_text(cfg: Config) -> str:
    count = len(_local_folder_paths(cfg))
    if count == 0:
        return "No local folder list yet. Refresh folders after choosing a base folder."
    return f"{count} local folders loaded"


def _setup_status_text(cfg: Config) -> str:
    if not _oauth_client_available(cfg):
        return "OAuth client missing. Install the packaged Catsync deb again or import a Google OAuth client."
    if not cfg.token_path.exists():
        return "Step 1: authorize Google Drive. After authorization, Catsync will load the folder list and switch to Sync."
    if not _local_folder_paths(cfg):
        return "Step 2: refresh folders, then choose the base folder and unchecked local folders on Sync."
    if cfg.dry_run or not cfg.allow_writes:
        return "Step 3: review the plan with dry-run before enabling writes."
    return "Setup complete. Catsync is configured for this computer."


def _format_epoch(value: int) -> str:
    dt = GLib.DateTime.new_from_unix_local(value)
    return dt.format("%Y-%m-%d %H:%M:%S") or str(value)


def _daemon_phase(db_path: Path) -> tuple[str, str | None]:
    data = _read_status_file(db_path)
    if not data:
        return "unknown", None
    updated_at = data.get("updated_at")
    updated = None
    if isinstance(updated_at, int):
        updated = _format_epoch(updated_at)
    return str(data.get("phase") or "unknown"), updated


def _read_status_file(db_path: Path) -> dict:
    status_path = Path(db_path).with_name("status.json")
    if not status_path.exists():
        return {}
    try:
        data = json.loads(status_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _current_activity(db_path: Path) -> dict | None:
    status = _read_status_file(db_path)
    activity = status.get("activity")
    if not isinstance(activity, dict):
        return None
    current = activity.get("current")
    return current if isinstance(current, dict) else None


def _short_activity(operation: dict) -> str:
    label = str(operation.get("label") or operation.get("action") or "activity")
    path = str(operation.get("path") or "")
    if len(path) > 64:
        path = f"...{path[-61:]}"
    return f"{label} {path}".strip()


def _format_status_time(value) -> str:
    if isinstance(value, int):
        return _format_epoch(value)
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
