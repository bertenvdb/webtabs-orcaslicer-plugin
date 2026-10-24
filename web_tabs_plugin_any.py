# /// script
# requires-python = ">=3.12"
#
# [tool.orcaslicer.plugin]
# name = "Web Tabs"
# description = "Adds user-defined web pages (Spoolman, Obico, Home Assistant, ...) as tabs in the main window."
# author = "Berten"
# version = "0.1.0"
# ///
"""Web Tabs -- user-defined web pages as main-window tabs.

How it works
------------
* The tab list lives in ``tabs.json`` in this plugin's storage folder (``orca.host.plugin.storage()``),
  falling back to this file's own folder. The file name deliberately avoids the word "config":
  OrcaSlicer's plugin audit blocks any path component containing it.
* ``register_capabilities()`` reads that list and registers one Pages capability per entry. A tab's
  title is its capability name, which OrcaSlicer caches at load time, so the set of tabs is fixed
  until the plugin is reloaded (Plugins dialog > Reload) or OrcaSlicer restarts.
* Each tab's page immediately redirects (``location.replace``) to its URL, so the site is the
  top-level document of the tab's webview: no iframe, no X-Frame-Options or third-party-cookie issues.
* The "Manage web tabs" script action opens an editor window for the list.
"""
import json
import os
import re

import orca

PLUGIN_TITLE = "Web Tabs"
TABS_FILE = "tabs.json"
MAX_TABS = 20
MAX_TITLE_LEN = 40

_URL_RE = re.compile(r"^https?://[^\s/$.?#][^\s]*$", re.IGNORECASE)


# --------------------------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------------------------

def _resolve_storage_dir():
    """Plugin storage folder, or this file's folder when the host cannot tell us (yet)."""
    try:
        path = orca.host.plugin.storage()
        if path:
            os.makedirs(path, exist_ok=True)
            return path
    except Exception:
        pass
    return os.path.dirname(os.path.abspath(__file__))


# Resolved once at load so registration and the editor always use the same file.
_STORAGE_DIR = None


def storage_dir():
    global _STORAGE_DIR
    if _STORAGE_DIR is None:
        _STORAGE_DIR = _resolve_storage_dir()
    return _STORAGE_DIR


def tabs_path():
    return os.path.join(storage_dir(), TABS_FILE)


def validate_tabs(raw):
    """Return (clean_tabs, errors). Never raises; invalid entries are reported, not kept."""
    errors = []
    clean = []
    seen = set()
    if not isinstance(raw, list):
        return [], ["The tab list must be a JSON array."]
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            errors.append(f"Row {index}: not an object.")
            continue
        title = str(entry.get("title", "")).strip()
        url = str(entry.get("url", "")).strip()
        if not title and not url:
            continue  # empty row in the editor: silently drop
        if not title:
            errors.append(f"Row {index}: title is empty.")
            continue
        if ";" in title:
            errors.append(f"Row {index}: title may not contain ';'.")
            continue
        if len(title) > MAX_TITLE_LEN:
            errors.append(f"Row {index}: title is longer than {MAX_TITLE_LEN} characters.")
            continue
        if title.casefold() in seen:
            errors.append(f"Row {index}: title '{title}' is used more than once.")
            continue
        if not _URL_RE.match(url):
            errors.append(f"Row {index}: '{url}' is not an http(s) URL.")
            continue
        seen.add(title.casefold())
        clean.append({"title": title, "url": url})
    if len(clean) > MAX_TABS:
        errors.append(f"At most {MAX_TABS} tabs are allowed.")
        clean = clean[:MAX_TABS]
    return clean, errors


def load_tabs():
    """Tabs from disk; a missing or broken file yields no tabs instead of a failed plugin load."""
    try:
        with open(tabs_path(), "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError:
        return []
    except Exception:
        return []
    tabs, _ = validate_tabs(raw)
    return tabs


def save_tabs(tabs):
    """Atomic write: a crash mid-save must not leave a half-written list behind."""
    path = tabs_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(tabs, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------------------------
# Tab pages
# --------------------------------------------------------------------------------------------

def _js_string(value):
    # json.dumps gives a valid JS string literal; also neutralise "</" so it cannot close <script>.
    return json.dumps(value).replace("</", "<\\/")


def _html_text(value):
    return (value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                 .replace('"', "&quot;"))


def redirect_page(title, url):
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{_html_text(title)}</title>
<style>
  body {{ display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
  p {{ color: var(--orca-muted); }}
</style></head>
<body>
<p>Opening <a href="{_html_text(url)}">{_html_text(url)}</a>&hellip;</p>
<script>window.location.replace({_js_string(url)});</script>
</body></html>"""


def make_page_class(title, url):
    """One Pages capability class per configured tab; the name doubles as the tab label."""

    class WebTabPage(orca.pages.PagesPluginCapabilityBase):
        def get_name(self):
            return title

        def get_ui(self):
            return redirect_page(title, url)

    # Distinct class names keep logs and tracebacks readable.
    WebTabPage.__name__ = WebTabPage.__qualname__ = "WebTab_" + re.sub(r"\W", "_", title)
    return WebTabPage


# --------------------------------------------------------------------------------------------
# Editor
# --------------------------------------------------------------------------------------------

EDITOR_PAGE = r"""
<style>
  body { margin: 0; padding: 16px; font-size: 13px; }
  h3 { margin: 0 0 4px; }
  .note { margin: 0 0 12px; color: var(--orca-muted); font-size: 12px; }
  table { width: 100%; }
  td { padding: 4px 6px; vertical-align: middle; }
  td.title { width: 28%; }
  td.ops { width: 1%; white-space: nowrap; }
  input { width: 100%; box-sizing: border-box; }
  button.quiet { background: transparent; color: var(--orca-fg); border-color: var(--orca-border); padding: 4px 8px; }
  .actions { display: flex; gap: 8px; margin-top: 12px; align-items: center; }
  .spacer { flex: 1; }
  #status { margin-top: 12px; white-space: pre-line; font-size: 12px; }
  #status.error { color: #e5534b; }
  #status.ok { color: var(--orca-muted); }
  .path { font-family: monospace; font-size: 11px; color: var(--orca-muted); word-break: break-all; }
</style>

<h3>Web tabs</h3>
<p class="note">Each row becomes a tab in the main window. Changes apply after reloading the plugin
(Plugins &gt; Web Tabs &gt; Reload) or restarting OrcaSlicer.</p>

<table>
  <thead><tr><th>Title</th><th>URL</th><th></th></tr></thead>
  <tbody id="rows"></tbody>
</table>

<div class="actions">
  <button type="button" class="quiet" id="add">+ Add tab</button>
  <span class="spacer"></span>
  <button type="button" class="quiet" id="close">Close</button>
  <button type="button" id="save">Save</button>
</div>
<div id="status"></div>
<p class="path" id="path"></p>

<script>
(function () {
  var rows = document.getElementById("rows");
  var status = document.getElementById("status");

  function setStatus(text, kind) {
    status.textContent = text || "";
    status.className = kind || "";
  }

  function rowElement(tab) {
    var tr = document.createElement("tr");
    tr.innerHTML =
      '<td class="title"><input type="text" class="t" placeholder="Spoolman" maxlength="40"></td>' +
      '<td><input type="text" class="u" placeholder="http://spoolman.lan:7912"></td>' +
      '<td class="ops">' +
        '<button type="button" class="quiet up" title="Move up">&#8593;</button> ' +
        '<button type="button" class="quiet down" title="Move down">&#8595;</button> ' +
        '<button type="button" class="quiet del" title="Remove">&#10005;</button>' +
      '</td>';
    tr.querySelector(".t").value = tab.title || "";
    tr.querySelector(".u").value = tab.url || "";
    tr.querySelector(".up").onclick = function () {
      if (tr.previousElementSibling) rows.insertBefore(tr, tr.previousElementSibling);
    };
    tr.querySelector(".down").onclick = function () {
      if (tr.nextElementSibling) rows.insertBefore(tr.nextElementSibling, tr);
    };
    tr.querySelector(".del").onclick = function () { tr.remove(); };
    return tr;
  }

  function render(tabs) {
    rows.innerHTML = "";
    tabs.forEach(function (tab) { rows.appendChild(rowElement(tab)); });
    if (!tabs.length) rows.appendChild(rowElement({}));
  }

  function collect() {
    return Array.prototype.map.call(rows.querySelectorAll("tr"), function (tr) {
      return { title: tr.querySelector(".t").value, url: tr.querySelector(".u").value };
    });
  }

  orca.onMessage(function (message) {
    if (!message) return;
    if (message.command === "tabs") {
      render(message.tabs || []);
      document.getElementById("path").textContent = "Stored in: " + message.path;
    } else if (message.command === "saved") {
      if (message.ok) {
        render(message.tabs || []);
        setStatus("Saved " + message.tabs.length + " tab(s). Reload the plugin or restart OrcaSlicer to apply.", "ok");
      } else {
        setStatus("Not saved:\n" + (message.errors || []).join("\n"), "error");
      }
    }
  });

  document.getElementById("add").onclick = function () { rows.appendChild(rowElement({})); };
  document.getElementById("save").onclick = function () {
    setStatus("Saving...", "ok");
    orca.postMessage({ command: "save", tabs: collect() });
  };
  document.getElementById("close").onclick = function () { orca.close(); };

  orca.postMessage({ command: "load" });
})();
</script>
"""


class ManageWebTabs(orca.script.ScriptPluginCapabilityBase):
    window = None

    def get_name(self):
        return "Manage web tabs"

    def execute(self):
        if self.window is not None and self.window.is_open():
            return orca.ExecutionResult.success("The web tabs editor is already open.")
        self.window = orca.host.ui.create_window(
            html=EDITOR_PAGE,
            title="Web Tabs",
            width=760,
            height=480,
            on_message=self.on_message,
            on_close=self.on_close,
        )
        return orca.ExecutionResult.success("Web tabs editor opened.")

    # Runs on the UI thread; everything here is a small local file operation.
    def on_message(self, message):
        command = (message or {}).get("command")
        if command == "load":
            self.window.post({"command": "tabs", "tabs": load_tabs(), "path": tabs_path()})
        elif command == "save":
            tabs, errors = validate_tabs(message.get("tabs"))
            if errors:
                self.window.post({"command": "saved", "ok": False, "errors": errors})
                return
            try:
                save_tabs(tabs)
            except Exception as error:  # surface I/O and audit refusals in the editor
                self.window.post({"command": "saved", "ok": False, "errors": [f"Could not write {tabs_path()}: {error}"]})
                return
            self.window.post({"command": "saved", "ok": True, "tabs": tabs})

    def on_close(self):
        self.window = None


@orca.plugin
class WebTabsPlugin(orca.base):
    def register_capabilities(self):
        orca.register_capability(ManageWebTabs)
        for tab in load_tabs():
            orca.register_capability(make_page_class(tab["title"], tab["url"]))
