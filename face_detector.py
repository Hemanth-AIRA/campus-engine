"""
face_detector.py — Lightweight face detection for LiveKit video frames.

Port of Sentinel's pico.js face detection to Python using OpenCV Haar cascade.
Runs directly on RGBA frames from LiveKit VideoStream in aira_bot_client.py.

Usage:
    detector = FaceDetector(interview_id=123, room_id="AIRA-...", process_id=1234)
    
    # Called every ~2s from _process_incoming_video:
    state_changed = detector.process_frame(rgba_bytes, width, height)
    if state_changed:
        # detector.is_present tells current state
        update_db(detector.is_present)
"""

import cv2
import numpy as np
import time
import logging
import os

logger = logging.getLogger("face_detector")

# ── Pico-equivalent thresholds (matched from Sentinel runner.cjs) ──
MISS_THRESHOLD = 3        # 3 consecutive misses → ABSENT (same as pico: 3 × 2s = 6s)
CHECK_INTERVAL_SEC = 2.0  # Check every 2 seconds
DETECT_WIDTH = 160        # Downscale to 160×120 for fast detection (same as pico)
DETECT_HEIGHT = 120
MIN_FACE_SIZE = 30        # Minimum face size in pixels (same as pico minsize)

# ── Best-face capture (two-tier) ──
BASELINE_WINDOW_S  = 20.0  # capture a baseline face during the first 20s of presence
CAPTURE_THROTTLE_S = 0.4   # cap detections to ~2.5/sec while a capture window is open
ANSWER_WINDOW_S    = 15.0  # default answer-capture window length


class FaceDetector:
    """
    Lightweight face presence detector using OpenCV Haar cascade.
    
    Equivalent to Sentinel's pico.js __startPresenceDetector:
    - Runs on 160×120 grayscale frames
    - Uses cascade classifier for face detection
    - State machine: PRESENT after 1 detection, ABSENT after 3 consecutive misses
    - Reports state changes for DB update
    """

    def __init__(self, interview_id=None, room_id=None, process_id=None):
        self.interview_id = interview_id
        self.room_id = room_id
        self.process_id = process_id

        # Load OpenCV Haar cascade (equivalent to pico facefinder)
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self.cascade = cv2.CascadeClassifier(cascade_path)
        if self.cascade.empty():
            logger.error(
                "[FaceDetector] Failed to load Haar cascade from %s",
                cascade_path,
            )
            raise RuntimeError(f"Cannot load cascade: {cascade_path}")

        # State machine (mirrors pico.js)
        self._state = None       # None = unknown, True = present, False = absent
        self._consecutive_misses = 0
        self._last_check_time = 0.0
        self._frame_count = 0
        self._detect_count = 0

        # ── Best-face capture (two-tier; replaces Sentinel's extraction) ──
        # BASELINE: best face during the first BASELINE_WINDOW_S of candidate
        #           presence (automatic, presence-driven).
        # ANSWER:   best face captured while the candidate answers Q1 & Q2 — the
        #           engine opens these windows via request_answer_capture().
        # Final best_face = ANSWER face if we got one, else the BASELINE face.
        self._baseline_face = None
        self._baseline_score = -1.0
        self._answer_face = None
        self._answer_score = -1.0
        self._present_since = None   # wall time the candidate first became present
        self._answer_until = 0.0     # wall time the current answer window ends
        self._last_capture_t = 0.0   # throttle detections
        self._capture_count = 0

        logger.info(
            "🛡 [process id: %s][room: %s] FaceDetector initialized "
            "(Haar cascade, %dx%d, interval=%ds, miss_threshold=%d)",
            process_id, room_id,
            DETECT_WIDTH, DETECT_HEIGHT,
            CHECK_INTERVAL_SEC, MISS_THRESHOLD,
        )

    @property
    def is_present(self) -> bool:
        """Current face presence state. True if face detected or unknown."""
        return self._state is not False

    @property
    def state_label(self) -> str:
        if self._state is None:
            return "UNKNOWN"
        return "PRESENT" if self._state else "ABSENT"

    def process_frame(self, rgba_bytes: bytes, width: int, height: int) -> bool:
        """
        Process an RGBA video frame for face detection.
        
        Should be called on every frame from LiveKit VideoStream.
        Internally throttles to CHECK_INTERVAL_SEC (~2s).
        
        Args:
            rgba_bytes: Raw RGBA pixel data
            width: Frame width
            height: Frame height
            
        Returns:
            True if state changed (PRESENT↔ABSENT), False otherwise.
            Check self.is_present for the new state.
        """
        now = time.time()

        # Throttle: only check every CHECK_INTERVAL_SEC
        if (now - self._last_check_time) < CHECK_INTERVAL_SEC:
            return False

        self._last_check_time = now
        self._detect_count += 1

        try:
            faces_found = self._detect_faces(rgba_bytes, width, height)
        except Exception as e:
            logger.warning(
                "⚠ [process id: %s][room: %s] FaceDetector error: %s",
                self.process_id, self.room_id, e,
            )
            return False

        old_state = self._state

        if faces_found > 0:
            # Face detected → instant PRESENT (same as pico)
            self._consecutive_misses = 0
            # Start the baseline window the first time the candidate appears.
            if self._present_since is None:
                self._present_since = now
            if self._state is not True:
                self._state = True
                logger.info(
                    "🛡 [process id: %s][room: %s] Face PRESENT "
                    "(faces=%d, check #%d)",
                    self.process_id, self.room_id,
                    faces_found, self._detect_count,
                )
        else:
            # No face → increment miss counter
            self._consecutive_misses += 1
            if self._consecutive_misses >= MISS_THRESHOLD and self._state is not False:
                self._state = False
                logger.warning(
                    "🛡 [process id: %s][room: %s] Face ABSENT "
                    "(misses=%d, check #%d)",
                    self.process_id, self.room_id,
                    self._consecutive_misses, self._detect_count,
                )

        return self._state != old_state

    def _detect_faces(self, rgba_bytes: bytes, width: int, height: int) -> int:
        """
        Detect faces in an RGBA frame using OpenCV Haar cascade.
        
        Mirrors pico.js detectFaces():
        1. Convert RGBA to grayscale
        2. Resize to 160×120
        3. Run cascade classifier
        4. Return number of faces found
        """
        # Convert RGBA bytes to numpy array
        frame = np.frombuffer(rgba_bytes, dtype=np.uint8).reshape((height, width, 4))

        # Convert RGBA to grayscale (same formula as pico.js)
        # pico: gray[i] = (2*R + 7*G + 1*B) / 10
        gray = cv2.cvtColor(frame, cv2.COLOR_RGBA2GRAY)

        # Resize to detection size (same as pico: 160×120)
        small = cv2.resize(gray, (DETECT_WIDTH, DETECT_HEIGHT))

        # Run Haar cascade (equivalent to pico.run_cascade + cluster_detections)
        faces = self.cascade.detectMultiScale(
            small,
            scaleFactor=1.1,      # pico: scalefactor=1.1
            minNeighbors=3,       # rough equivalent to pico's confidence > 5.0 filter
            minSize=(MIN_FACE_SIZE, MIN_FACE_SIZE),
        )

        return len(faces)

    # ── Best-face capture API ────────────────────────────────────────────────

    def request_answer_capture(self, duration_s: float = ANSWER_WINDOW_S):
        """Open an ANSWER-capture window (~duration_s) covering the candidate
        answering a question. Faces captured here are PREFERRED over the baseline.
        The engine calls this while the candidate answers Q1 & Q2."""
        self._answer_until = max(self._answer_until, time.time() + duration_s)

    def maybe_capture_best_face(self, rgba_bytes: bytes, width: int, height: int):
        """Score this frame's face into whichever capture window is open:
          • ANSWER window (engine-opened during Q1/Q2 answers) — preferred, OR
          • BASELINE window (first 20s of presence) — fallback.
        Throttled, and a cheap no-op when no window is open, so it's safe to call
        on every video frame."""
        now = time.time()
        in_answer = now < self._answer_until
        in_baseline = (self._present_since is not None
                       and (now - self._present_since) <= BASELINE_WINDOW_S)
        if not in_answer and not in_baseline:
            return
        if (now - self._last_capture_t) < CAPTURE_THROTTLE_S:
            return
        self._last_capture_t = now
        try:
            jpeg, score = self._best_crop_and_score(rgba_bytes, width, height)
        except Exception as e:
            logger.debug("[FaceDetector] capture error: %s", e)
            return
        if jpeg is None:
            return
        # ANSWER takes priority when both windows overlap.
        if in_answer:
            if score > self._answer_score:
                self._answer_face, self._answer_score = jpeg, score
                self._capture_count += 1
                logger.info(
                    "🛡 [process id: %s][room: %s] Best ANSWER face updated "
                    "(score=%.0f, #%d, %d bytes)",
                    self.process_id, self.room_id, score, self._capture_count, len(jpeg),
                )
        elif score > self._baseline_score:
            self._baseline_face, self._baseline_score = jpeg, score
            logger.info(
                "🛡 [process id: %s][room: %s] Baseline face updated "
                "(score=%.0f, %d bytes)",
                self.process_id, self.room_id, score, len(jpeg),
            )

    def _best_crop_and_score(self, rgba_bytes: bytes, width: int, height: int):
        """Full-res detect → largest face → padded crop → (jpeg, quality_score).
        Score = face_area × sharpness (Laplacian variance) so we prefer big,
        in-focus faces."""
        frame = np.frombuffer(rgba_bytes, dtype=np.uint8).reshape((height, width, 4))
        bgr = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        # minSize relative to frame size: candidate video can arrive small (e.g.
        # 240x180 after LiveKit downscale), where a fixed 80px floor misses faces
        # the presence check (160x120, minSize 30) still finds. Floor at 40px.
        ms = max(40, int(min(width, height) * 0.18))
        faces = self.cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(ms, ms))
        if len(faces) == 0:
            return None, -1.0
        areas = [fw * fh for (x, y, fw, fh) in faces]
        (x, y, fw, fh) = faces[int(np.argmax(areas))]
        pad_x, pad_y = int(fw * 0.2), int(fh * 0.2)
        x1, y1 = max(0, x - pad_x), max(0, y - pad_y)
        x2, y2 = min(width, x + fw + pad_x), min(height, y + fh + pad_y)
        crop = bgr[y1:y2, x1:x2]
        if crop.size == 0:
            return None, -1.0
        sharp = cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        score = float(fw * fh) * float(sharp)
        ok, jpeg = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            return None, -1.0
        return jpeg.tobytes(), score

    @property
    def best_face_jpeg(self):
        """JPEG bytes of the best candidate face: the answer-time face if we
        captured one (preferred), else the first-20s baseline face, else None."""
        return self._answer_face if self._answer_face is not None else self._baseline_face

    def get_best_face_crop(self, rgba_bytes: bytes, width: int, height: int):
        """
        Detect faces at full resolution and return the best face crop as JPEG bytes.
        Used for best_face capture during interview.
        
        Returns:
            (jpeg_bytes, bbox) or (None, None) if no face found.
        """
        frame = np.frombuffer(rgba_bytes, dtype=np.uint8).reshape((height, width, 4))
        bgr = cv2.cvtColor(frame, cv2.COLOR_RGBA2BGR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

        faces = self.cascade.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(80, 80),  # Larger minimum for quality crop
        )

        if len(faces) == 0:
            return None, None

        # Pick largest face
        areas = [w * h for (x, y, w, h) in faces]
        best_idx = np.argmax(areas)
        (x, y, fw, fh) = faces[best_idx]

        # Add padding (20%)
        pad_x = int(fw * 0.2)
        pad_y = int(fh * 0.2)
        x1 = max(0, x - pad_x)
        y1 = max(0, y - pad_y)
        x2 = min(width, x + fw + pad_x)
        y2 = min(height, y + fh + pad_y)

        face_crop = bgr[y1:y2, x1:x2]
        _, jpeg = cv2.imencode(".jpg", face_crop, [cv2.IMWRITE_JPEG_QUALITY, 90])

        return jpeg.tobytes(), (x1, y1, x2, y2)
