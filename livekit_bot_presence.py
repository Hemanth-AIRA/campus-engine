"""
livekit_bot_presence.py

When AIRA_USE_LIVEKIT=true, the Python bot connects ONLY to the LiveKit SFU.
It never sends a Socket.IO 'join' event to the Connect server — so the room
state machine never registers the bot, and the UI doesn't show the bot as a
participant.

This module sends a lightweight Socket.IO presence signal so that:
  1. The server room state includes the bot (triggers room:timer, room:state broadcast)
  2. The candidate browser UI shows the bot as a participant
  3. wait_for_participant() can verify the bot is in the room

All audio is still handled by AiraBotClient / LiveKit — this is PURELY presence.

Usage (in aira_3_webrtc_orchestration.py, after LiveKit adapter connects):
    from livekit_bot_presence import BotPresenceClient
    presence = BotPresenceClient(base_url, bot_join_url, room_id)
    presence.connect_background(timeout=20)
    # ... run interview ...
    presence.disconnect()

The bot_join_url must contain ?t=<signed-JWT> with claims:
    { roomId, role='bot', jti }
"""

import logging
import threading
import time
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)


def _extract_token_from_url(join_url: str) -> str:
    """Extract the ?t= JWT from a bot join URL like:
       http://localhost:3001/?t=eyJ...&room=ROOM-ID&role=bot
    """
    try:
        parsed = urlparse(join_url)
        params = parse_qs(parsed.query)
        tokens = params.get("t", [])
        return tokens[0] if tokens else ""
    except Exception as e:
        logger.debug("BotPresence: could not extract token from URL: %s", e)
        return ""


class BotPresenceClient:
    """
    Lightweight Socket.IO client that joins the Connect room as the bot role.

    Establishes the bot's presence in the server room state WITHOUT running
    a browser. Audio is handled separately by AiraBotClient via LiveKit.

    Protocol:
        Client connects with  auth: { t: <JWT> }
        Server middleware verifies the JWT (roomId + role='bot' + jti required)
        Client emits  'join'  with  { roomId, name, consent: true }
        Server replies 'join-ok' and broadcasts 'room:state' to all in room
    """

    def __init__(self, base_url: str, bot_join_url: str, room_id: str,
                 bot_name: str = "AIRA Interviewer"):
        """
        Args:
            base_url:     Connect server URL, e.g. "http://localhost:3001"
            bot_join_url: The bot's interview join URL — must contain ?t=<JWT>
            room_id:      The interview room ID string
            bot_name:     Display name shown in the participant list
        """
        self.base_url = base_url.rstrip("/")
        self.bot_join_url = bot_join_url
        self.room_id = room_id
        self.bot_name = bot_name

        self._sio = None
        self._thread = None
        self._stop_event = threading.Event()
        self._joined_event = threading.Event()
        self._connected = False

    # ─────────────────────────────────────────────────────────────
    # PUBLIC API
    # ─────────────────────────────────────────────────────────────

    def connect_background(self, timeout: float = 20.0) -> bool:
        """
        Start the Socket.IO presence connection in a background daemon thread.
        Blocks up to `timeout` seconds waiting for the 'join-ok' confirmation.

        Returns True if the bot joined the room successfully, False otherwise.
        """
        token = _extract_token_from_url(self.bot_join_url)
        if not token:
            logger.warning(
                "⚠️ [room %s] BotPresence: no ?t= JWT in bot_join_url — "
                "bot will NOT appear in room participant list",
                self.room_id,
            )
            return False

        self._stop_event.clear()
        self._joined_event.clear()

        # Import here to avoid a hard dependency — fail gracefully if not installed
        try:
            import socketio as sio_lib
        except ImportError:
            logger.warning(
                "⚠️ [room %s] BotPresence: 'python-socketio[client]' not installed — "
                "pip install 'python-socketio[client]' to show bot in UI",
                self.room_id,
            )
            return False

        sio = sio_lib.Client(
            reconnection=True,
            reconnection_attempts=3,
            reconnection_delay=2,
            reconnection_delay_max=10,
            logger=False,
            engineio_logger=False,
        )
        self._sio = sio

        room_id = self.room_id
        bot_name = self.bot_name
        joined_event = self._joined_event

        # ── Event handlers ──────────────────────────────────────

        @sio.event
        def connect():
            logger.info(
                "🔌 [room %s] BotPresence: Socket.IO connected (sid=%s)",
                room_id, sio.sid,
            )
            # Server auth middleware already verified the JWT from handshake.auth.t.
            # Now emit 'join' with the room context — matches what the browser bot sends.
            sio.emit("join", {
                "roomId": room_id,
                "name": bot_name,
                "consent": True,
            })
            logger.info("➡️ [room %s] BotPresence: 'join' emitted", room_id)

        @sio.on("join-ok")
        def on_join_ok(data):
            others_count = len(data.get("others", [])) if isinstance(data, dict) else 0
            logger.info(
                "✅ [room %s] BotPresence: 'join-ok' received — "
                "bot is now visible in room state (others in room: %d)",
                room_id, others_count,
            )
            self._connected = True
            joined_event.set()

        @sio.on("join-denied")
        def on_join_denied(data):
            reason = (
                data.get("reason", "unknown") if isinstance(data, dict)
                else str(data)
            )
            logger.error(
                "❌ [room %s] BotPresence: 'join-denied' — reason=%s — "
                "bot will NOT appear in room participant list",
                room_id, reason,
            )
            joined_event.set()  # unblock caller even on denial

        @sio.on("kicked")
        def on_kicked(data):
            logger.warning("⚠️ [room %s] BotPresence: kicked — %s", room_id, data)
            self._connected = False

        @sio.on("meeting:ended")
        def on_meeting_ended(_data):
            logger.info("🏁 [room %s] BotPresence: meeting:ended received", room_id)
            self._connected = False

        @sio.event
        def disconnect():
            logger.info("🔌 [room %s] BotPresence: Socket.IO disconnected", room_id)
            self._connected = False

        @sio.event
        def connect_error(err):
            logger.error(
                "❌ [room %s] BotPresence: connection error — %s",
                room_id, err,
            )
            joined_event.set()  # unblock caller

        # python-socketio uses ONLY scheme+host from the URL and appends
        # `socketio_path` (default "socket.io") — it ignores the URL's path. So when
        # Connect is mounted under a prefix (prod: https://hubx.airahr.ai/connect) we
        # must fold that prefix into socketio_path → "/connect/socket.io", otherwise
        # it hits "/socket.io" (the frontend → HTML) and the handshake fails.
        # Localhost/dev (no prefix) → "socket.io".
        from urllib.parse import urlparse
        _u = urlparse(self.base_url)
        _origin = f"{_u.scheme}://{_u.netloc}"
        _prefix = _u.path.rstrip("/")
        _sio_path = f"{_prefix}/socket.io" if _prefix else "socket.io"

        # ── Background thread ────────────────────────────────────

        def _run():
            try:
                # Pass the JWT in Socket.IO handshake auth — server reads from
                # socket.handshake.auth.t (see server.cjs middleware line 1136)
                sio.connect(
                    _origin,
                    auth={"t": token},
                    socketio_path=_sio_path,
                    transports=["websocket"],
                    wait_timeout=10,
                )
                # Stay alive until stop requested
                while not self._stop_event.is_set():
                    time.sleep(1)
            except Exception as e:
                logger.error(
                    "❌ [room %s] BotPresence thread error: %s",
                    room_id, e,
                )
                joined_event.set()  # unblock caller
            finally:
                try:
                    if sio.connected:
                        sio.disconnect()
                except Exception:
                    pass

        self._thread = threading.Thread(
            target=_run,
            daemon=True,
            name=f"bot-presence-{room_id}",
        )
        self._thread.start()

        # Block until join-ok / join-denied / error / timeout
        if not joined_event.wait(timeout=timeout):
            logger.warning(
                "⚠️ [room %s] BotPresence: timed out waiting for join-ok "
                "(%.0fs). Bot may not appear in participant list.",
                room_id, timeout,
            )

        return self._connected

    def disconnect(self):
        """Stop the presence connection cleanly."""
        logger.info("🔌 [room %s] BotPresence: disconnecting...", self.room_id)
        self._stop_event.set()
        try:
            if self._sio and self._sio.connected:
                self._sio.disconnect()
        except Exception as e:
            logger.debug("[room %s] BotPresence disconnect error: %s", self.room_id, e)
        if self._thread:
            self._thread.join(timeout=5)
        self._connected = False
        logger.info("✅ [room %s] BotPresence: disconnected", self.room_id)

    @property
    def is_connected(self) -> bool:
        return self._connected
