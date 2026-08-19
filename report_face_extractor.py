import cv2
import os
import cv2
import logging
import os
import numpy as np
from datetime import datetime
logger = logging.getLogger(__name__)

def rename_folder_with_timestamp(folder_path):
    """
    Renames the given folder by appending the current timestamp (YYYYMMDD_HHMMSS).
    Returns the new folder path.
    """
    if not os.path.isdir(folder_path):
        logger.info(f"Folder does not exist, skipping rename: {folder_path}")
        return None
    base_dir = os.path.dirname(folder_path)
    folder_name = os.path.basename(folder_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    new_folder_name = f"{folder_name}_{timestamp}"
    new_folder_path = os.path.join(base_dir, new_folder_name)
    try:
        os.rename(folder_path, new_folder_path)
        logger.info(f"Renamed folder '{folder_path}' to '{new_folder_path}'")
        return new_folder_path
    except Exception as e:
        logger.error(f"Failed to rename folder: {e}")
        return None

def cleanup_png_files_with_prefix(folder, file_name_prefix):
    """
    Deletes all PNG files in the specified folder that start with the given prefix.
    """
    removed = 0
    for fname in os.listdir(folder):
        if fname.startswith(file_name_prefix) and fname.lower().endswith('.png'):
            path = os.path.join(folder, fname)
            try:
                os.remove(path)
                removed += 1
            except Exception as e:
                logger.error(f"Failed to remove {path}: {e}")
    logger.info(f"Removed {removed} PNG files with prefix '{file_name_prefix}' from '{folder}'.")

def load_dnn_face_detector():
    modelFile = "res10_300x300_ssd_iter_140000.caffemodel"
    configFile = "deploy.prototxt"
    if not os.path.exists(modelFile) or not os.path.exists(configFile):
        logger.info("Downloading model files...")
        import urllib.request
        urllib.request.urlretrieve(
            "https://raw.githubusercontent.com/opencv/opencv/master/samples/dnn/face_detector/deploy.prototxt",
            configFile)
        urllib.request.urlretrieve(
            "https://github.com/opencv/opencv_3rdparty/raw/dnn_samples_face_detector_20170830/res10_300x300_ssd_iter_140000.caffemodel",
            modelFile)

    return cv2.dnn.readNetFromCaffe(configFile, modelFile)

def extract_best_face(input_folder, source_folder_name, source_file_name_prefix, output_folder="best_faces", conf_threshold=0.7):
    net = load_dnn_face_detector()
    face_folder = os.path.join(output_folder, str(source_folder_name))
    os.makedirs(face_folder, exist_ok=True)

    input_folder_name = os.path.join(str(input_folder), str(source_folder_name))
    image_files = [f for f in os.listdir(input_folder_name) if f.startswith(source_file_name_prefix) and f.lower().endswith('.png')]
    saved_count = 0
    logger.info(f"Extracting best faces from {len(image_files)} images in {input_folder_name} with prefix '{source_file_name_prefix}'")

    for file_name in image_files:
        image_path = os.path.join(input_folder_name, file_name)
        img = cv2.imread(image_path)
        if img is None:
            continue

        h, w = img.shape[:2]
        blob = cv2.dnn.blobFromImage(img, 1.0, (300, 300), [104, 117, 123], False, False)
        net.setInput(blob)
        detections = net.forward()

        best_face = None
        best_conf = 0
        best_area = 0

        for i in range(detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            if confidence < conf_threshold:
                continue

            box = detections[0, 0, i, 3:7] * [w, h, w, h]
            (x1, y1, x2, y2) = box.astype("int")
            area = (x2 - x1) * (y2 - y1)
            if confidence > best_conf or (confidence == best_conf and area > best_area):
                # Add padding (e.g., 30% of box height/width)
                pad_x = int(0.2 * (x2 - x1))
                pad_y = int(0.2 * (y2 - y1))

                # Expand box with padding
                x1_p = max(0, x1 - pad_x)
                y1_p = max(0, y1 - pad_y)
                x2_p = min(w, x2 + pad_x)
                y2_p = min(h, y2 + pad_y)

                # Crop with padding
                face_crop = img[y1_p:y2_p, x1_p:x2_p]

                best_face = face_crop
                best_conf = confidence
                best_area = area

        if best_face is not None:
            output_path = os.path.join(output_folder, str(source_folder_name) ,f"{os.path.splitext(file_name)[0]}_best_face.png")
            cv2.imwrite(output_path, best_face)
            saved_count += 1
            # logger.info(f"Saved best face to: {output_path}")

    logger.info(f"\nTotal best face images saved: {saved_count}")



def compute_sharpness(image):
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.Laplacian(gray, cv2.CV_64F).var()

def compute_brightness(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    return np.mean(hsv[:, :, 2])  # Value channel

def score_image(image):
    sharpness = compute_sharpness(image)
    brightness = compute_brightness(image)

    # Normalize values and compute a simple score
    if brightness < 30 or brightness > 230:
        brightness_score = 0  # too dark or too bright
    else:
        brightness_score = 1

    return sharpness * brightness_score  # prioritize sharp, well-lit faces

def select_best_face(source_folder_name,source_file_name_prefix, face_folder="best_faces"):
    extract_best_face("captured_tiles",source_folder_name,source_file_name_prefix)
    best_score = -1
    best_image_path = None
    
    face_folder_interview_id = os.path.join(face_folder, str(source_folder_name))
    logger.info(f"Searching for best face images in: {face_folder_interview_id}")
    for fname in os.listdir(face_folder_interview_id):
        if fname.lower().endswith(".png"):
            path = os.path.join(face_folder_interview_id, fname)
            image = cv2.imread(path)
            if image is None:
                continue

            score = score_image(image)
            if score > best_score:
                best_score = score
                best_image_path = path

    if best_image_path:
        rel_path = os.path.relpath(best_image_path, os.getcwd())
        if not rel_path.startswith('.'):
            rel_path = f'./{rel_path}'
        logger.info(f"\n🏆 Best face image: {rel_path} (score: {best_score:.2f})")
        return rel_path
    else:
        logger.error("No valid face images found.")
        
        
# Example usage
# if __name__ == "__main__":
#     file_name_prefix="zoom_tile_87"
#     select_best_face(file_name_prefix, "best_faces")
