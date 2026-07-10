"""Debug Obsidian (and other WebView apps) on a USB-connected phone.

Two platforms, one CLI (``omd``):

- ``omd ios ...``     drives the WKWebView via pymobiledevice3 (Web Inspector +
  AFC/house_arrest) - read state, deploy a plugin build, reload, capture logs.
- ``omd android ...`` drives the Chromium WebView via adb + Chrome DevTools
  Protocol - the same surface, over a standard CDP endpoint.

Transports (pymobiledevice3, adb, websockets) are imported lazily so ``--help``
and argument parsing work without a device connected.
"""

__version__ = "0.1.0"
