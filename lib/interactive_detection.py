from logging import getLogger, basicConfig, DEBUG, INFO
import os
import sys
from lib import detectors
import cv2
import math
import numpy as np
from timeit import default_timer as timer
from time import sleep
import configparser
import threading
from queue import Queue

# load configuration
config = configparser.ConfigParser()
config.read('tello.cfg')
det_interval = eval(config.get('detection', 'det_interval'))

logger = getLogger(__name__)

basicConfig(
    level=INFO,
    format="%(asctime)s %(levelname)s %(name)s %(funcName)s(): %(message)s")

FP32 = "extension/IR/FP32/"
FP16 = "extension/IR/FP16/"

model_ss_xml = "MobileNetSSD_deploy.xml"
model_fc_xml = "face-detection-retail-0004.xml"
model_ag_xml = "age-gender-recognition-retail-0013.xml"
model_em_xml = "emotions-recognition-retail-0003.xml"
model_hp_xml = "head-pose-estimation-adas-0001.xml"
# facial-landmarks-35-adas-0001.xml does not work well with MYRIAD device
#model_lm_xml = "facial-landmarks-35-adas-0001.xml"
model_lm_xml = "landmarks-regression-retail-0009.xml"


class Detectors(object):
    def __init__(self, devices, models, cpu_extension, plugin_dir,
                 prob_threshold, prob_threshold_face, is_async_mode):
        self.cpu_extension = cpu_extension
        self.plugin_dir = plugin_dir
        self.prob_threshold = prob_threshold
        self.prob_threshold_face = prob_threshold_face
        self.is_async_mode = is_async_mode
        self._load_detectors(devices, models)

    def _load_detectors(self, devices, models):
        device_ss, device_fc, device_ag, device_em, device_hp, device_lm = devices
        # self.models are used by app.py to display using models
        self.models = self._define_models(devices, models)
        model_ss, model_fc, model_ag, model_em, model_hp, model_lm = self.models
        cpu_extension = self.cpu_extension
        plugin_dir = self.plugin_dir
        prob_threshold = self.prob_threshold
        prob_threshold_face = self.prob_threshold_face
        is_async_mode = self.is_async_mode

        # Create MobileNet-SSD detection class instance
        self.ssd_detection = detectors.SSDetection(
            device_ss, model_ss, cpu_extension, plugin_dir, prob_threshold,
            is_async_mode)
        # Create face_detection class instance
        self.face_detectors = detectors.FaceDetection(
            device_fc, model_fc, cpu_extension, plugin_dir,
            prob_threshold_face, is_async_mode)
        # Create face_analytics class instances
        self.age_gender_detectors = detectors.AgeGenderDetection(
            device_ag, model_ag, cpu_extension, plugin_dir,
            prob_threshold_face, is_async_mode)
        self.emotions_detectors = detectors.EmotionsDetection(
            device_em, model_em, cpu_extension, plugin_dir,
            prob_threshold_face, is_async_mode)
        self.headpose_detectors = detectors.HeadPoseDetection(
            device_hp, model_hp, cpu_extension, plugin_dir, prob_threshold,
            is_async_mode)
        self.facial_landmarks_detectors = detectors.FacialLandmarksDetection(
            device_lm, model_lm, cpu_extension, plugin_dir,
            prob_threshold_face, is_async_mode)

    def _define_models(self, devices, models):
        device_ss, device_fc, device_ag, device_em, device_hp, device_lm = devices
        model_ss, model_fc, model_ag, model_em, model_hp, model_lm = models

        # set devices and models
        fp_path = FP32 if device_ss == "CPU" else FP16
        model_ss = fp_path + model_ss_xml if model_ss is None else model_ss
        fp_path = FP32 if device_fc == "CPU" else FP16
        model_fc = fp_path + model_fc_xml if model_fc is None else model_fc
        fp_path = FP32 if device_ag == "CPU" else FP16
        model_ag = fp_path + model_ag_xml if model_ag is None else model_ag
        fp_path = FP32 if device_em == "CPU" else FP16
        model_em = fp_path + model_em_xml if model_em is None else model_em
        fp_path = FP32 if device_hp == "CPU" else FP16
        model_hp = fp_path + model_hp_xml if model_hp is None else model_hp
        fp_path = FP32 if device_lm == "CPU" else FP16
        model_lm = fp_path + model_lm_xml if model_lm is None else model_lm

        return [model_ss, model_fc, model_ag, model_em, model_hp, model_lm]


class Detections(Detectors):
    def __init__(self, devices, models, cpu_extension, plugin_dir,
                 prob_threshold, prob_threshold_face, is_async_mode):
        super().__init__(devices, models, cpu_extension, plugin_dir,
                         prob_threshold, prob_threshold_face, is_async_mode)

        frame = None
        next_frame = None
        is_async_mode = None
        is_age_gender_detection = None
        is_emotions_detection = None
        is_head_pose_detection = None
        is_facial_landmarks_detection = None
        self.det_frame = None

        self.o = threading.Thread(
            target=self._start_object_detection,
            args=(frame, next_frame, is_async_mode))
        self.o.start()
        self.f = threading.Thread(
            target=self._start_face_detection,
            args=(frame, next_frame, is_async_mode, is_age_gender_detection,
                  is_emotions_detection, is_head_pose_detection,
                  is_facial_landmarks_detection))
        self.f.start()

        # initialize Calculate FPS
        self.accum_time = 0
        self.curr_fps = 0
        self.fps = "FPS: ??"
        self.prev_time = timer()

    def get_det_objects(self, frame, next_frame, is_async_mode):
        if not self.o.is_alive():
            self.o = threading.Thread(
                target=self._start_object_detection,
                args=(frame, next_frame, is_async_mode))
            self.o.start()
        return self.det_frame

    def get_det_faces(self, frame, next_frame, is_async_mode,
                      is_age_gender_detection, is_emotions_detection,
                      is_head_pose_detection, is_facial_landmarks_detection):
        if not self.f.is_alive():
            self.f = threading.Thread(
                target=self._start_face_detection,
                args=(frame, next_frame, is_async_mode,
                      is_age_gender_detection, is_emotions_detection,
                      is_head_pose_detection, is_facial_landmarks_detection))
            self.f.start()
        return self.det_frame

    def _start_object_detection(self, *args):
        frame = args[0]
        next_frame = args[1]
        is_async_mode = args[2]

        if frame is None:
            logger.info("frame is None")
            return

        det_time = 0
        det_time_txt = ""

        det_time, frame = self.ssd_detection.object_inference(
            frame, next_frame, is_async_mode)
        self.det_frame = self.draw_perf_stats(det_time, det_time_txt, frame,
                                              is_async_mode)
        sleep(det_interval)

    def _start_face_detection(self, *args):

        # ----------- Start Face Detection ---------- #
        logger.debug("** face_detection start **")

        frame = args[0]
        next_frame = args[1]
        is_async_mode = args[2]
        is_age_gender_detection = args[3]
        is_emotions_detection = args[4]
        is_head_pose_detection = args[5]
        is_facial_landmarks_detection = args[6]

        color = (0, 255, 0)
        det_time = 0
        det_time_fc = 0
        det_time_txt = ""

        if frame is None:
            logger.info("frame is None")
            return

        frame_h, frame_w = frame.shape[:2]  # shape (h, w, c)
        is_face_analytics_enabled = True if is_age_gender_detection or is_emotions_detection else False

        inf_start = timer()
        self.face_detectors.submit_req(frame, next_frame, is_async_mode)
        ret = self.face_detectors.wait()
        faces = self.face_detectors.get_results(is_async_mode)
        inf_end = timer()
        det_time = inf_end - inf_start
        det_time_fc = det_time

        face_count = faces.shape[2]
        det_time_txt = "face_cnt:{} face:{:.2f} ".format(face_count,
                                                            det_time * 1000)

        # ----------- Start Face Analytics ---------- #

        face_id = 0
        face_w, face_h = 0, 0
        face_frame = None
        next_face_frame = None
        prev_box = None
        det_time_ag = 0
        det_time_em = 0
        det_time_hp = 0
        det_time_lm = 0

        # Run face analytics with async mode when detected faces count are lager than 1.
        if is_async_mode and face_count > 1:
            is_face_async_mode = True
        else:
            is_face_async_mode = False

        if is_face_async_mode:
            face_count = face_count + 1

        face_q = Queue()
        for face in faces[0][0]:
            face_q.put(face)

        for face_id in range(face_count):
            face_id = 0
            face_analytics = ""
            age_gender = ""
            emotion = ""
            head_pose = ""
            facial_landmark = ""

            if not face_q.empty():
                face = face_q.get()

            box = face[3:7] * np.array([frame_w, frame_h, frame_w, frame_h])
            xmin, ymin, xmax, ymax = box.astype("int")
            class_id = int(face[1])
            result = str(face_id) + " " + str(round(face[2] * 100, 1)) + '% '

            if xmin < 0 or ymin < 0:
                logger.info(
                    "Rapid motion returns negative value(xmin and ymin) which make face_frame None. xmin:{} xmax:{} ymin:{} ymax:{}".
                    format(xmin, xmax, ymin, ymax))
                return frame

            # Start face analytics
            # prev_box is previous box(faces), which is None at the first time
            # will be updated with prev face box in async mode
            if is_face_async_mode:
                next_face_frame = frame[ymin:ymax, xmin:xmax]
                ##if next_face_frame is None:
                ##    return frame
                if prev_box is not None:
                    xmin, ymin, xmax, ymax = prev_box.astype("int")
            else:
                face_frame = frame[ymin:ymax, xmin:xmax]

            # Check face frame.
            # face_fame is None at the first time with async mode.
            if face_frame is not None:
                face_w, face_h = face_frame.shape[:2]
                # Resizing face_frame will be failed when witdh or height of the face_fame is 0 ex. (243, 0, 3)
                if face_w == 0 or face_h == 0:
                    logger.error(
                        "Unexpected shape of face frame. face_frame.shape:{} {}".
                        format(face_h, face_w))
                    return frame

            # ----------- Start Age/Gender detection ---------- #
            if is_age_gender_detection:
                logger.debug("*** age_gender_detection start ***")

                inf_start = timer()
                self.age_gender_detectors.submit_req(
                    face_frame, next_face_frame, is_face_async_mode)
                ret = self.age_gender_detectors.wait()
                age, gender = self.age_gender_detectors.get_results(
                    is_face_async_mode)
                age_gender = str(int(round(age))) + " " + gender + " "
                inf_end = timer()
                det_time = inf_end - inf_start

                det_time_ag += det_time
                logger.debug("age:{} gender:{}".format(age, gender))
                logger.debug("*** age_gender_detection end ***")

            # ----------- Start Emotions detection ---------- #
            if is_emotions_detection:
                logger.debug("*** emotions detection start ***")

                inf_start = timer()
                self.emotions_detectors.submit_req(face_frame, next_face_frame,
                                                   is_face_async_mode)
                ret = self.emotions_detectors.wait()
                emotion = self.emotions_detectors.get_results(
                    is_face_async_mode)
                emotion = emotion + " "
                inf_end = timer()
                det_time = inf_end - inf_start

                det_time_em += det_time
                logger.debug("emotion:{}".format(emotion))
                logger.debug("*** emotion_detection end ***")

            # ----------- Start Head Pose detection ---------- #
            if is_head_pose_detection:
                logger.debug("*** head_pose_detection start ***")

                inf_start = timer()
                self.headpose_detectors.submit_req(face_frame, next_face_frame,
                                                   is_face_async_mode)
                ret = self.headpose_detectors.wait()
                yaw, pitch, roll = self.headpose_detectors.get_results(
                    is_face_async_mode)
                # face h/w will be 0 at the first inference with async mode
                if face_h != 0 and face_w != 0:
                    center_of_face = (xmin + face_h / 2, ymin + face_w / 2, 0)
                    frame = self.draw_axes(frame, center_of_face, yaw, pitch,
                                           roll, 50)
                inf_end = timer()
                det_time = inf_end - inf_start

                det_time_hp += det_time
                logger.debug("yaw(z):{:f}, pitch(y):{:f} roll(x):{:f}".format(
                    yaw, pitch, roll))
                logger.debug("*** head_pose_detection end ***")

            # ----------- Start facial landmarks detection ---------- #
            if is_facial_landmarks_detection:
                logger.debug("*** landmarks_detection start ***")

                inf_start = timer()
                self.facial_landmarks_detectors.submit_req(
                    face_frame, next_face_frame, is_face_async_mode)
                ret = self.facial_landmarks_detectors.wait()
                normed_landmarks = self.facial_landmarks_detectors.get_results(
                    is_face_async_mode)
                n_lm = normed_landmarks.size
                for i in range(int(n_lm / 2)):
                    normed_x = normed_landmarks[2 * i]
                    normed_y = normed_landmarks[2 * i + 1]
                    x_lm = xmin + face_h * normed_x
                    y_lm = ymin + face_w * normed_y
                    cv2.circle(frame, (int(x_lm), int(y_lm)),
                               1 + int(0.012 * face_h), (0, 255, 255), -1)
                inf_end = timer()
                det_time = inf_end - inf_start

                det_time_lm += det_time
                logger.debug("*** landmarks_detection end ***")

            face_id += 1

            if is_face_async_mode:
                face_frame = next_face_frame
                prev_box = box

            face_analytics = age_gender + emotion

            cv2.rectangle(frame, (xmin, ymin - 17), (xmax, ymin), color, -1)
            cv2.rectangle(frame, (xmin, ymin - 17), (xmax, ymin),
                          (255, 255, 255))
            # Draw box and label\class_id
            cv2.rectangle(frame, (xmin, ymin), (xmax, ymax), (0, 255, 0))
            if is_face_analytics_enabled:
                cv2.putText(frame, face_analytics, (xmin + 3, ymin - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 0), 1)
            else:
                cv2.putText(frame, result, (xmin + 3, ymin - 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 0), 1)
            logger.debug("face_id:{} confidence:{}%".format(
                face_id, round(face[2] * 100)))

        det_time = det_time_fc + det_time_ag + det_time_em + det_time_hp + det_time_lm
        det_time_txt = det_time_txt + "ag:{:.2f} ".format(det_time_ag * 1000)
        det_time_txt = det_time_txt + "em:{:.2f} ".format(det_time_em * 1000)
        det_time_txt = det_time_txt + "hp:{:.2f} ".format(det_time_hp * 1000)
        det_time_txt = det_time_txt + "lm:{:.2f} ".format(det_time_lm * 1000)

        self.det_frame = self.draw_perf_stats(det_time, det_time_txt, frame,
                                              is_async_mode)
        sleep(det_interval)

    def draw_axes(self, frame, center_of_face, yaw, pitch, roll, scale):
        yaw *= np.pi / 180.0
        pitch *= np.pi / 180.0
        roll *= np.pi / 180.0

        cx = int(center_of_face[0])
        cy = int(center_of_face[1])

        Rx = np.array([[1, 0, 0], [0, math.cos(pitch), -math.sin(pitch)],
                       [0, math.sin(pitch), math.cos(pitch)]])
        Ry = np.array([[math.cos(yaw), 0, -math.sin(yaw)], [0, 1, 0],
                       [math.sin(yaw), 0, math.cos(yaw)]])
        Rz = np.array([[math.cos(roll), -math.sin(roll), 0],
                       [math.sin(roll), math.cos(roll), 0], [0, 0, 1]])
        R = Rz @ Ry @ Rx  # R = np.dot(Rz, np.dot(Ry, Rx))

        camera_matrix = self.build_camera_matrix(center_of_face, 950.0)

        xaxis = np.array(([1 * scale, 0, 0]), dtype='float32').reshape(3, 1)
        yaxis = np.array(([0, -1 * scale, 0]), dtype='float32').reshape(3, 1)
        zaxis = np.array(([0, 0, -1 * scale]), dtype='float32').reshape(3, 1)
        zaxis1 = np.array(([0, 0, 1 * scale]), dtype='float32').reshape(3, 1)

        o = np.array(([0, 0, 0]), dtype='float32').reshape(3, 1)
        o[2] = camera_matrix[0][0]

        xaxis = np.dot(R, xaxis) + o
        yaxis = np.dot(R, yaxis) + o
        zaxis = np.dot(R, zaxis) + o
        zaxis1 = np.dot(R, zaxis1) + o

        xp2 = (xaxis[0] / xaxis[2] * camera_matrix[0][0]) + cx
        yp2 = (xaxis[1] / xaxis[2] * camera_matrix[1][1]) + cy
        p2 = (int(xp2), int(yp2))
        cv2.line(frame, (cx, cy), p2, (0, 0, 255), 2)

        xp2 = (yaxis[0] / yaxis[2] * camera_matrix[0][0]) + cx
        yp2 = (yaxis[1] / yaxis[2] * camera_matrix[1][1]) + cy
        p2 = (int(xp2), int(yp2))
        cv2.line(frame, (cx, cy), p2, (0, 255, 0), 2)

        xp1 = (zaxis1[0] / zaxis1[2] * camera_matrix[0][0]) + cx
        yp1 = (zaxis1[1] / zaxis1[2] * camera_matrix[1][1]) + cy
        p1 = (int(xp1), int(yp1))
        xp2 = (zaxis[0] / zaxis[2] * camera_matrix[0][0]) + cx
        yp2 = (zaxis[1] / zaxis[2] * camera_matrix[1][1]) + cy
        p2 = (int(xp2), int(yp2))

        cv2.line(frame, p1, p2, (255, 0, 0), 2)
        cv2.circle(frame, p2, 3, (255, 0, 0), 2)

        return frame

    def build_camera_matrix(self, center_of_face, focal_length):
        cx = int(center_of_face[0])
        cy = int(center_of_face[1])
        camera_matrix = np.zeros((3, 3), dtype='float32')
        camera_matrix[0][0] = focal_length
        camera_matrix[0][2] = cx
        camera_matrix[1][1] = focal_length
        camera_matrix[1][2] = cy
        camera_matrix[2][2] = 1
        return camera_matrix

    def draw_perf_stats(self, det_time, det_time_txt, frame, is_async_mode):

        # Draw FPS in top left corner
        fps = self.calc_fps()
        cv2.rectangle(frame, (frame.shape[1] - 50, 0), (frame.shape[1], 17),
                      (255, 255, 255), -1)
        cv2.putText(frame, fps, (frame.shape[1] - 50 + 3, 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 0, 0), 1)

        # Draw performance stats
        if is_async_mode:
            inf_time_message = "Total Inference time: {:.3f} ms for async mode".format(
                det_time * 1000)
        else:
            inf_time_message = "Total Inference time: {:.3f} ms for sync mode".format(
                det_time * 1000)
        cv2.putText(frame, inf_time_message, (10, 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 10, 10), 1)
        if det_time_txt:
            inf_time_message_each = "Detction time: {}".format(det_time_txt)
            cv2.putText(frame, inf_time_message_each, (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 10, 10), 1)
        return frame

    def calc_fps(self):
        curr_time = timer()
        exec_time = curr_time - self.prev_time
        self.prev_time = curr_time
        self.accum_time = self.accum_time + exec_time
        self.curr_fps = self.curr_fps + 1

        if self.accum_time > 1:
            self.accum_time = self.accum_time - 1
            self.fps = "FPS: " + str(self.curr_fps)
            self.curr_fps = 0

        return self.fps
