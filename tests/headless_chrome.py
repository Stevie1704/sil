"""A headless Chrome for browser checks, driven over its DevTools pipe.

Only the standard library: Chrome reads DevTools commands from fd 3 and
writes replies to fd 4, NUL-separated JSON (`--remote-debugging-pipe`).
Each browser has its own temporary profile, so it never touches a running
Chrome. Network access is off: every request other than `file:` and `data:`
is failed and counted, so a page that tries the network is visible.

`find_chrome()` returns None when no browser is installed; tests skip then.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
)


def find_chrome() -> str | None:
    configured = os.environ.get("SIL_TEST_CHROME")
    for candidate in ((configured,) if configured else CANDIDATES):
        found = shutil.which(candidate) or (
            candidate if os.access(candidate, os.X_OK) else None)
        if found:
            return found
    return None


class Chrome:
    """One headless browser with one page."""

    def __init__(self, executable: str, *, width: int = 1280,
                 height: int = 900):
        self.profile = tempfile.mkdtemp(prefix="sil-chrome-")
        to_chrome_r, self._to_chrome = os.pipe()
        self._from_chrome, from_chrome_w = os.pipe()
        os.set_inheritable(to_chrome_r, True)
        os.set_inheritable(from_chrome_w, True)

        def arrange_fds():
            # Move both ends above 4 first, so neither dup2 closes the other.
            reader = fcntl.fcntl(to_chrome_r, fcntl.F_DUPFD, 10)
            writer = fcntl.fcntl(from_chrome_w, fcntl.F_DUPFD, 10)
            os.dup2(reader, 3)
            os.dup2(writer, 4)

        self.process = subprocess.Popen(
            [executable, "--headless=new", "--remote-debugging-pipe",
             f"--user-data-dir={self.profile}", "--no-first-run",
             "--no-default-browser-check", "--disable-gpu",
             "--disable-extensions", "--disable-sync",
             "--disable-background-networking",
             "--host-resolver-rules=MAP * ~NOTFOUND",
             "--proxy-server=127.0.0.1:9", f"--window-size={width},{height}",
             "about:blank"],
            preexec_fn=arrange_fds,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=False)
        os.close(to_chrome_r)
        os.close(from_chrome_w)
        self._buffer = b""
        self._next_id = 0
        self.events: list[dict] = []
        self.blocked_requests: list[str] = []
        self.console_errors: list[str] = []
        targets = self.send("Target.getTargets")["targetInfos"]
        page = next(t for t in targets if t["type"] == "page")
        self.session = self.send("Target.attachToTarget",
                                 targetId=page["targetId"],
                                 flatten=True)["sessionId"]
        for domain in ("Page.enable", "Runtime.enable"):
            self.page(domain)
        self.page("Fetch.enable", patterns=[{"urlPattern": "*"}])
        self.resize(width, height)

    def resize(self, width: int, height: int = 900) -> None:
        self.page("Emulation.setDeviceMetricsOverride", width=width,
                  height=height, deviceScaleFactor=1, mobile=width < 700)

    # --- protocol --------------------------------------------------------

    def _write(self, message: dict) -> None:
        os.write(self._to_chrome, json.dumps(message).encode() + b"\0")

    def _read(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while b"\0" not in self._buffer:
            if time.monotonic() > deadline:
                raise TimeoutError("Chrome did not answer")
            chunk = os.read(self._from_chrome, 1 << 16)
            if not chunk:
                raise RuntimeError("Chrome closed its DevTools pipe")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\0", 1)
        return json.loads(line)

    def _handle_event(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params", {})
        if method == "Fetch.requestPaused":
            url = params["request"]["url"]
            allowed = url.startswith(("file:", "data:", "blob:"))
            if not allowed:
                self.blocked_requests.append(url)
            self._write({
                "id": self._new_id(), "sessionId": message.get("sessionId"),
                "method": "Fetch.continueRequest" if allowed
                else "Fetch.failRequest",
                "params": {"requestId": params["requestId"]} | (
                    {} if allowed else {"errorReason": "InternetDisconnected"}),
            })
        elif method == "Runtime.exceptionThrown":
            self.console_errors.append(
                params["exceptionDetails"].get("text", "exception"))
        elif method == "Runtime.consoleAPICalled" and params.get("type") == \
                "error":
            self.console_errors.append(str(params.get("args")))
        self.events.append(message)

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def send(self, method: str, session: str | None = None,
             timeout: float = 30, **params: Any) -> dict:
        number = self._new_id()
        message = {"id": number, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        self._write(message)
        while True:
            reply = self._read(timeout)
            if reply.get("id") == number:
                if "error" in reply:
                    raise RuntimeError(f"{method}: {reply['error']}")
                return reply.get("result", {})
            if "method" in reply:
                self._handle_event(reply)

    def page(self, method: str, **params: Any) -> dict:
        return self.send(method, self.session, **params)

    # --- conveniences ----------------------------------------------------

    def open(self, path: Path, *, javascript: bool = True,
             scheme: str = "light", media: str = "") -> None:
        self.page("Emulation.setScriptExecutionDisabled", value=not javascript)
        self.page("Emulation.setEmulatedMedia", media=media, features=[
            {"name": "prefers-color-scheme", "value": scheme}])
        self.events.clear()
        self.page("Page.navigate", url=path.resolve().as_uri())
        self.wait_for("Page.loadEventFired")

    def wait_for(self, method: str, timeout: float = 30) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            for event in self.events:
                if event.get("method") == method:
                    self.events.remove(event)
                    return event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(method)
            self._handle_event(self._read(remaining))

    def evaluate(self, expression: str) -> Any:
        result = self.page("Runtime.evaluate", expression=expression,
                           returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")

    def press(self, key: str, code: str | None = None,
              key_code: int | None = None, text: str | None = None) -> None:
        params = {"key": key, "code": code or key,
                  "windowsVirtualKeyCode": key_code or 0}
        down = {"type": "keyDown", **params}
        if text:
            down["text"] = text
        self.page("Input.dispatchKeyEvent", **down)
        self.page("Input.dispatchKeyEvent", type="keyUp", **params)

    def tab(self) -> None:
        self.press("Tab", key_code=9)

    def screenshot(self, path: Path, full_page: bool = True,
                   max_height: int | None = None) -> None:
        params: dict[str, Any] = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True
            metrics = self.page("Page.getLayoutMetrics")["cssContentSize"]
            params["clip"] = {"x": 0, "y": 0, "width": metrics["width"],
                              "height": min(metrics["height"],
                                            max_height or metrics["height"]),
                              "scale": 1}
        data = self.page("Page.captureScreenshot", **params)["data"]
        path.write_bytes(base64.b64decode(data))

    def pdf(self) -> bytes:
        data = self.page("Page.printToPDF", printBackground=True)["data"]
        return base64.b64decode(data)

    def close(self) -> None:
        try:
            self._write({"id": self._new_id(), "method": "Browser.close"})
            self.process.wait(timeout=10)
        except Exception:  # noqa: BLE001 - make sure it is gone
            self.process.kill()
            self.process.wait()
        finally:
            os.close(self._to_chrome)
            os.close(self._from_chrome)
            shutil.rmtree(self.profile, ignore_errors=True)
