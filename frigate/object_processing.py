import json
import hashlib
import datetime
import time
import copy
import cv2
import threading
import numpy as np
from collections import Counter, defaultdict
import itertools
import pyarrow.plasma as plasma
import matplotlib.pyplot as plt
from frigate.util import draw_box_with_label, PlasmaManager
from frigate.edgetpu import load_labels

PATH_TO_LABELS = '/labelmap.txt'

LABELS = load_labels(PATH_TO_LABELS)
cmap = plt.cm.get_cmap('tab10', len(LABELS.keys()))

COLOR_MAP = {}
for key, val in LABELS.items():
    COLOR_MAP[val] = tuple(int(round(255 * c)) for c in cmap(key)[:3])

def zone_filtered(obj, object_config):
    object_name = obj['label']
    object_filters = object_config.get('filters', {})

    if object_name in object_filters:
        obj_settings = object_filters[object_name]

        # if the min area is larger than the
        # detected object, don't add it to detected objects
        if obj_settings.get('min_area',-1) > obj['area']:
            return True
        
        # if the detected object is larger than the
        # max area, don't add it to detected objects
        if obj_settings.get('max_area', 24000000) < obj['area']:
            return True

        # if the score is lower than the threshold, skip
        if obj_settings.get('threshold', 0) > obj['score']:
            return True
        
    return False

class TrackedObjectProcessor(threading.Thread):
    def __init__(self, camera_config, zone_config, client, topic_prefix, tracked_objects_queue):
        threading.Thread.__init__(self)
        self.camera_config = camera_config
        self.zone_config = zone_config
        self.client = client
        self.topic_prefix = topic_prefix
        self.tracked_objects_queue = tracked_objects_queue
        self.camera_data = defaultdict(lambda: {
            'best_objects': {},
            'object_status': defaultdict(lambda: defaultdict(lambda: 'OFF')),
            'tracked_objects': {},
            'current_frame': np.zeros((720,1280,3), np.uint8),
            'current_frame_time': 0.0,
            'object_id': None
        })
        self.zone_data = defaultdict(lambda: {
            'object_status': defaultdict(lambda: defaultdict(lambda: 'OFF')),
            'contours': {}
        })

        # create zone contours
        for name, config in zone_config.items():
            for camera, camera_zone_config in config.items():
                coordinates = camera_zone_config['coordinates']
                if isinstance(coordinates, list):
                    self.zone_data[name]['contours'][camera] =  np.array([[int(p.split(',')[0]), int(p.split(',')[1])] for p in coordinates])
                elif isinstance(coordinates, str):
                    points = coordinates.split(',')
                    self.zone_data[name]['contours'][camera] =  np.array([[int(points[i]), int(points[i+1])] for i in range(0, len(points), 2)])
                else:
                    print(f"Unable to parse zone coordinates for {name} - {camera}")
        
        # set colors for zones
        colors = plt.cm.get_cmap('tab10', len(self.zone_data.keys()))
        for i, zone in enumerate(self.zone_data.values()):
            zone['color'] = tuple(int(round(255 * c)) for c in colors(i)[:3])

        self.plasma_client = PlasmaManager()
        
    def get_best(self, camera, label):
        if label in self.camera_data[camera]['best_objects']:
            return self.camera_data[camera]['best_objects'][label]['frame']
        else:
            return None
    
    def get_current_frame(self, camera):
        return self.camera_data[camera]['current_frame']

    def run(self):
        while True:
            camera, frame_time, tracked_objects = self.tracked_objects_queue.get()

            camera_config = self.camera_config[camera]
            best_objects = self.camera_data[camera]['best_objects']
            current_object_status = self.camera_data[camera]['object_status']
            self.camera_data[camera]['tracked_objects'] = tracked_objects
            self.camera_data[camera]['current_frame_time'] = frame_time

            # build a dict of objects in each zone for current camera
            current_objects_in_zones = defaultdict(lambda: [])
            for obj in tracked_objects.values():
                bottom_center = (obj['centroid'][0], obj['box'][3])
                # check each zone
                for name, zone in self.zone_data.items():
                    # check each camera with a contour for the zone
                    for camera, contour in zone['contours'].items():
                        if cv2.pointPolygonTest(contour, bottom_center, False) >= 0 and not zone_filtered(obj, self.zone_config[name][camera].get('filters', {})):
                            current_objects_in_zones[name].append(obj['label'])

            ###
            # Draw tracked objects on the frame
            ###
            current_frame = self.plasma_client.get(f"{camera}{frame_time}")

            if not current_frame is plasma.ObjectNotAvailable:
                # draw the bounding boxes on the frame
                for obj in tracked_objects.values():
                    thickness = 2
                    color = COLOR_MAP[obj['label']]
                    
                    if obj['frame_time'] != frame_time:
                        thickness = 1
                        color = (255,0,0)

                    # draw the bounding boxes on the frame
                    box = obj['box']
                    draw_box_with_label(current_frame, box[0], box[1], box[2], box[3], obj['label'], f"{int(obj['score']*100)}% {int(obj['area'])}", thickness=thickness, color=color)
                    # draw the regions on the frame
                    region = obj['region']
                    cv2.rectangle(current_frame, (region[0], region[1]), (region[2], region[3]), (0,255,0), 1)
                
                if camera_config['snapshots']['show_timestamp']:
                    time_to_show = datetime.datetime.fromtimestamp(frame_time).strftime("%m/%d/%Y %H:%M:%S")
                    cv2.putText(current_frame, time_to_show, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, fontScale=.8, color=(255, 255, 255), thickness=2)

                if camera_config['snapshots']['draw_zones']:
                    for name, zone in self.zone_data.items():
                        thickness = 2 if len(current_objects_in_zones[name]) == 0 else 8
                        if camera in zone['contours']:
                            cv2.drawContours(current_frame, [zone['contours'][camera]], -1, zone['color'], thickness)

                ###
                # Set the current frame
                ###
                self.camera_data[camera]['current_frame'] = current_frame

                # delete the previous frame from the plasma store and update the object id
                if not self.camera_data[camera]['object_id'] is None:
                    self.plasma_client.delete(self.camera_data[camera]['object_id'])
                self.camera_data[camera]['object_id'] = f"{camera}{frame_time}"
            
            ###
            # Maintain the highest scoring recent object and frame for each label
            ###
            for obj in tracked_objects.values():
                # if the object wasn't seen on the current frame, skip it
                if obj['frame_time'] != frame_time:
                    continue
                if obj['label'] in best_objects:
                    now = datetime.datetime.now().timestamp()
                    # if the object is a higher score than the current best score 
                    # or the current object is more than 1 minute old, use the new object
                    if obj['score'] > best_objects[obj['label']]['score'] or (now - best_objects[obj['label']]['frame_time']) > 60:
                        obj['frame'] = np.copy(self.camera_data[camera]['current_frame'])
                        best_objects[obj['label']] = obj
                else:
                    obj['frame'] = np.copy(self.camera_data[camera]['current_frame'])
                    best_objects[obj['label']] = obj

            ###
            # Report over MQTT
            ###
            # get the zones that are relevant for this camera
            relevant_zones = [zone for zone, config in self.zone_config.items() if camera in config]
            # for each zone
            for zone in relevant_zones:
                # create the set of labels in the current frame and previously reported
                labels_for_zone = set(current_objects_in_zones[zone] + list(self.zone_data[zone]['object_status'][camera].keys()))
                # for each label
                for label in labels_for_zone:
                    # compute the current 'ON' vs 'OFF' status by checking if any camera sees the object in the zone
                    previous_state = any([camera[label] == 'ON' for camera in self.zone_data[zone]['object_status'].values()])
                    self.zone_data[zone]['object_status'][camera][label] = 'ON' if label in current_objects_in_zones[zone] else 'OFF'
                    new_state = any([camera[label] == 'ON' for camera in self.zone_data[zone]['object_status'].values()])
                    # if the value is changing, send over MQTT
                    if previous_state == False and new_state == True:
                        self.client.publish(f"{self.topic_prefix}/{zone}/{label}", 'ON', retain=False)
                    elif previous_state == True and new_state == False:
                        self.client.publish(f"{self.topic_prefix}/{zone}/{label}", 'OFF', retain=False)

            # count objects with more than 2 entries in history by type
            obj_counter = Counter()
            for obj in tracked_objects.values():
                if len(obj['history']) > 1:
                    obj_counter[obj['label']] += 1
                    
            # report on detected objects
            for obj_name, count in obj_counter.items():
                new_status = 'ON' if count > 0 else 'OFF'
                if new_status != current_object_status[obj_name]:
                    current_object_status[obj_name] = new_status
                    self.client.publish(f"{self.topic_prefix}/{camera}/{obj_name}", new_status, retain=False)
                    # send the best snapshot over mqtt
                    best_frame = cv2.cvtColor(best_objects[obj_name]['frame'], cv2.COLOR_RGB2BGR)
                    ret, jpg = cv2.imencode('.jpg', best_frame)
                    if ret:
                        jpg_bytes = jpg.tobytes()
                        self.client.publish(f"{self.topic_prefix}/{camera}/{obj_name}/snapshot", jpg_bytes, retain=True)

            # expire any objects that are ON and no longer detected
            expired_objects = [obj_name for obj_name, status in current_object_status.items() if status == 'ON' and not obj_name in obj_counter]
            for obj_name in expired_objects:
                current_object_status[obj_name] = 'OFF'
                self.client.publish(f"{self.topic_prefix}/{camera}/{obj_name}", 'OFF', retain=False)
                # send updated snapshot over mqtt
                best_frame = cv2.cvtColor(best_objects[obj_name]['frame'], cv2.COLOR_RGB2BGR)
                ret, jpg = cv2.imencode('.jpg', best_frame)
                if ret:
                    jpg_bytes = jpg.tobytes()
                    self.client.publish(f"{self.topic_prefix}/{camera}/{obj_name}/snapshot", jpg_bytes, retain=True)
