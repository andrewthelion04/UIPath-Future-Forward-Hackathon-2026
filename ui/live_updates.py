"""WebSocket în backend-ul Streamlit; UI-ul se rerulează numai la o invalidare."""
import json
import threading

import streamlit as st
from websockets.sync.client import connect

from ui import api_client as api


class UpdateFeed:
    def __init__(self, url, token):
        self.version = None
        self.status = "Connecting"
        self._stop = threading.Event()
        self._socket = None
        self._thread = threading.Thread(target=self._listen, args=(url, token), daemon=True)
        self._thread.start()

    def _listen(self, url, token):
        delay = 1
        while not self._stop.is_set():
            try:
                with connect(url, open_timeout=5, close_timeout=1, proxy=None) as socket:
                    self._socket = socket
                    socket.send(json.dumps({"type":"authenticate", "token":token or ""}))
                    for raw in socket:
                        if self._stop.is_set():
                            return
                        event = json.loads(raw)
                        if event.get("type") in ("snapshot", "changed", "heartbeat"):
                            self.version = event["version"]
                            self.status = "Live"
                            delay = 1
            except Exception:
                self.status = "Reconnecting"
            finally:
                self._socket = None
                if not self._stop.is_set():
                    self.status = "Reconnecting"
            if self._stop.wait(delay):
                return
            delay = min(delay * 2, 30)

    def close(self):
        self._stop.set()
        if self._socket:
            self._socket.close()


@st.cache_resource(max_entries=1, on_release=lambda current: current.close(), show_spinner=False)
def feed(url, token):
    return UpdateFeed(url, token)


@st.fragment(run_every="1s")
def live_status():
    url = api.API_URL.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/ws/updates"
    current = feed(url, api.api_token())
    st.caption(f"Updates: {current.status}")
    previous = st.session_state.get("_live_version")
    if current.version is not None and previous != current.version:
        st.session_state["_live_version"] = current.version
        st.rerun()
