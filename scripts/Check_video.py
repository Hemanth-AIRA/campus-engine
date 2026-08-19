import pyautogui
import cv2
import numpy as np
import time
import inspect
import pyautogui
import cv2
import numpy as np
import time
import datetime
import os
import logging
import threading
logger = logging.getLogger(__name__)


# Set your final tile region from earlier test
VIDEO_TILE_REGION = (616, 89, 492, 882)

# face_net = cv2.dnn.readNetFromCaffe(
#     "deploy.prototxt", 
#     "res10_300x300_ssd_iter_140000_fp16.caffemodel"
# )

# def detect_face_dnn(image):
#     h, w = image.shape[:2]
#     blob = cv2.dnn.blobFromImage(image, 1.0, (300, 300),
#                                  (104.0, 177.0, 123.0))
#     face_net.setInput(blob)
#     detections = face_net.forward()

#     for i in range(detections.shape[2]):
#         confidence = detections[0, 0, i, 2]
#         if confidence > 0.6:
#             logger.info("👤 Detected face with DNN.")
#             return True
#     return False


# Load Haar cascade for face detection
face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")



# Ensure the directory exists
CAPTURE_DIR = "captured_tiles"
os.makedirs(CAPTURE_DIR, exist_ok=True)


def capture_zoom_tile(interview_id, region=None):
    
    region=None
    # Capture full screen if no region is specified
    screenshot = pyautogui.screenshot() if region is None else pyautogui.screenshot(region=region)
    image = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

    # Save the image
    # Timestamped filename
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # Create a subdirectory for the interview_id inside CAPTURE_DIR
    interview_dir = os.path.join(CAPTURE_DIR, str(interview_id))
    os.makedirs(interview_dir, exist_ok=True)
    file_path = os.path.join(interview_dir, f"zoom_tile_{interview_id}_{timestamp}.png")
    cv2.imwrite(file_path, image)
    logger.info(f"🖼️ Saved full-screen image to {file_path}")

    return image
    # screenshot = pyautogui.screenshot(region=region)
    # image = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

    # # Generate filename with timestamp
    # timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # file_path = os.path.join(CAPTURE_DIR, f"zoom_tile_{timestamp}.png")

    # # Save the image
    # cv2.imwrite(file_path, image)
    # logger.info(f"🖼️ Saved captured image to {file_path}")

    # return image
    # # return cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2BGR)

def detect_face_only(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    # gray = cv2.equalizeHist(gray)
    # faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5)
    faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=8, minSize=(60, 60))
    if len(faces) > 0:
        logger.info(f"👤 Detected faces in tile: {len(faces)}")
        return True
    return False


# Load the pre-trained model (once, globally)
net = cv2.dnn.readNetFromCaffe(
    "models/MobileNetSSD_deploy.prototxt",
    "models/MobileNetSSD_deploy.caffemodel"
)

# List of class labels that SSD was trained on
CLASSES = ["background", "aeroplane", "bicycle", "bird", "boat",
           "bottle", "bus", "car", "cat", "chair", "cow", "diningtable",
           "dog", "horse", "motorbike", "person", "pottedplant",
           "sheep", "sofa", "train", "tvmonitor"]


def detect_person(image, conf_threshold=0.5):
    (h, w) = image.shape[:2]
    blob = cv2.dnn.blobFromImage(cv2.resize(image, (300, 300)), 
                                 0.007843, (300, 300), 127.5)
    net.setInput(blob)
    detections = net.forward()

    person_count = 0
    for i in range(detections.shape[2]):
        confidence = detections[0, 0, i, 2]
        idx = int(detections[0, 0, i, 1])
        if confidence > conf_threshold and CLASSES[idx] == "person":
            person_count += 1

    # logger.info(f"🚶 Detected people in tile: {person_count}")
    return person_count > 0

def check_video_on(interview, retries=5, delay=2):

    for i in range(retries):
        start_time = time.time()
        logger.info(f"Checking participant video... ({i+1}/{retries})")
        tile_image = capture_zoom_tile(interview['id'])
        # if detect_face_dnn(tile_image):
        # if detect_face_only(tile_image):
        if detect_person(tile_image):
            end_time = time.time()
            elapsed = end_time - start_time
            logger.info(f"Time check:  ✅ Participant video is ON.: {elapsed:.2f} seconds")
            return True
        else:
            end_time = time.time()
            elapsed = end_time - start_time
            logger.info(f"Time check: ❌ Participant video appears to be OFF. : {elapsed:.2f} seconds")
        if i < retries - 1:
            time.sleep(delay)
        end_time = time.time()
        elapsed = end_time - start_time
    logger.info(f"Time check: ⛔ Video was not detected after all retrie: {elapsed:.2f} seconds")
    return False


def start_async_capture(interview_id, capture_interval=20, stop_event=None):
    """
    Start asynchronous image capture that takes screenshots every minute during interview
    
    Args:
        interview_id: ID of the interview for file naming
        capture_interval: Seconds between captures (default 60 = 1 minute)
        stop_event: Threading event to stop the capture
    """
    if stop_event is None:
        stop_event = threading.Event()
    
    logger.info(f"🎥 Starting asynchronous image capture for interview {interview_id} (every {capture_interval}s)")
    capture_count = 0
    
    while not stop_event.is_set():
        try:
            # Capture image
            capture_zoom_tile(interview_id)
            capture_count += 1
            logger.info(f"📸 Async capture #{capture_count} completed for interview {interview_id}")
            
            # Wait for next capture (or until stop event is set)
            stop_event.wait(capture_interval)
            
        except Exception as e:
            logger.error(f"❌ Error in async capture for interview {interview_id}: {e}")
            # Continue capturing even if one fails
            time.sleep(5)  # Brief pause before retry
    
    logger.info(f"🛑 Stopped asynchronous capture for interview {interview_id}. Total captures: {capture_count}")


def start_capture_thread(interview_id, capture_interval=60):
    """
    Start the asynchronous capture in a separate thread
    
    Returns:
        tuple: (thread, stop_event) - Use stop_event.set() to stop the capture
    """
    stop_event = threading.Event()
    capture_thread = threading.Thread(
        target=start_async_capture,
        args=(interview_id, capture_interval, stop_event),
        daemon=True,
        name=f"AsyncCapture-{interview_id}"
    )
    capture_thread.start()
    logger.info(f"🚀 Started async capture thread for interview {interview_id}")
    return capture_thread, stop_event

