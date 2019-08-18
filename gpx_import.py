#!/usr/bin/env python

from datetime import datetime, timedelta

import dateutil.parser
import numpy as np
import os
from flask import flash
from geopy.distance import distance
from lxml import objectify

from _import import postprocess_move
from filters import degree_to_radian, radian_to_degree
from model import db, Device, Move, Sample

# Import options
GPX_IMPORT_OPTION_PAUSE_DETECTION = 'gpx_option_pause_detection'
GPX_IMPORT_OPTION_PAUSE_DETECTION_THRESHOLD = 'gpx_option_pause_detection_threshold'
GPX_IMPORT_PAUSE_TYPE_PAUSE_DETECTION = 'pause_detection'

# General constants
GPX_DEVICE_NAME = 'GPX import'
GPX_DEVICE_SERIAL = 'GPX_IMPORT'
GPX_SAMPLE_TYPE = 'gps-base'
GPX_ACTIVITY_TYPE = 'Unknown activity'

# GPX XML attributes, names
GPX_NAMESPACES = {
    '1.0': '{http://www.topografix.com/GPX/1/0}',
    '1.1': '{http://www.topografix.com/GPX/1/1}'
}

GPX_TRK = 'trk'
GPX_TRKSEG = 'trkseg'
GPX_TRKPT = 'trkpt'
GPX_TRKPT_ATTRIB_LATITUDE = 'lat'
GPX_TRKPT_ATTRIB_LONGITUDE = 'lon'
GPX_TRKPT_ATTRIB_ELEVATION = 'ele'

GPX_NAMESPACE_TRACKPOINTEXTENSION_V1 = '{http://www.garmin.com/xmlschemas/TrackPointExtension/v1}'
GPX_EXTENSION_TRACKPOINTEXTENSION = 'TrackPointExtension'
GPX_EXTENSION_TRACKPOINTEXTENSION_HEARTRATE = 'hr'

GPX_EXTENSION_GPX_V1_TEMP = 'temp'
GPX_EXTENSION_GPX_V1_DISTANCE = 'distance'
GPX_EXTENSION_GPX_V1_ALTITUDE = 'altitude'
GPX_EXTENSION_GPX_V1_ENERGY = 'energy'
GPX_EXTENSION_GPX_V1_SEALEVELPRESSURE = 'seaLevelPressure'
GPX_EXTENSION_GPX_V1_SPEED = 'speed'
GPX_EXTENSION_GPX_V1_VSPEED = 'verticalSpeed'


def parse_sample_extensions(sample, track_point):
    if hasattr(track_point, 'extensions'):
        for extension in track_point.extensions.iterchildren():
            if extension.tag == GPX_NAMESPACE_TRACKPOINTEXTENSION_V1 + GPX_EXTENSION_TRACKPOINTEXTENSION:
                if hasattr(extension, GPX_EXTENSION_TRACKPOINTEXTENSION_HEARTRATE):
                    sample.hr = float(extension.hr) / 60.0  # BPM

            for namespace in GPX_NAMESPACES.values():
                if extension.tag.startswith(namespace):
                    tag = extension.tag.replace(namespace, '')
                    text = extension.text
                    if tag == GPX_EXTENSION_GPX_V1_TEMP:
                        sample.temperature = float(text) + 273.15  # Kelvin
                    elif tag == GPX_EXTENSION_GPX_V1_DISTANCE:
                        sample.distance = float(text)
                    elif tag == GPX_EXTENSION_GPX_V1_ALTITUDE:
                        sample.gps_altitude = float(text)
                        sample.altitude = int(round(sample.gps_altitude))
                    elif tag == GPX_EXTENSION_GPX_V1_ENERGY:
                        sample.energy_consumption = float(text)
                    elif tag == GPX_EXTENSION_GPX_V1_SEALEVELPRESSURE:
                        sample.sea_level_pressure = float(text)
                    elif tag == GPX_EXTENSION_GPX_V1_SPEED:
                        sample.speed = float(text)
                    elif tag == GPX_EXTENSION_GPX_V1_VSPEED:
                        sample.vertical_speed = float(text)
                    break


def parse_samples(tree, move, gpx_namespace, import_options):
    all_samples = []

    tracks = tree.iterchildren(tag=gpx_namespace + GPX_TRK)
    for track in tracks:
        track_samples = []

        track_segments = track.iterchildren(tag=gpx_namespace + GPX_TRKSEG)
        for track_segment in track_segments:
            segment_samples = []

            track_points = track_segment.iterchildren(tag=gpx_namespace + GPX_TRKPT)
            for track_point in track_points:
                sample = Sample()
                sample.move = move

                # GPS position / altitude
                sample.latitude = degree_to_radian(float(track_point.attrib[GPX_TRKPT_ATTRIB_LATITUDE]))
                sample.longitude = degree_to_radian(float(track_point.attrib[GPX_TRKPT_ATTRIB_LONGITUDE]))
                sample.sample_type = GPX_SAMPLE_TYPE
                if hasattr(track_point, GPX_TRKPT_ATTRIB_ELEVATION):
                    sample.gps_altitude = float(track_point.ele)
                    sample.altitude = int(round(sample.gps_altitude))

                # Time / UTC
                sample.utc = dateutil.parser.parse(str(track_point.time))

                # Option flags
                pause_detected = False

                # Track segment samples
                if len(segment_samples) > 0:
                    # Accumulate time delta to previous sample to get the total duration
                    time_delta = sample.utc - segment_samples[-1].utc
                    sample.time = segment_samples[-1].time + time_delta

                    # Accumulate distance to previous sample
                    distance_delta = distance((radian_to_degree(sample.latitude), radian_to_degree(sample.longitude)),
                                              (radian_to_degree(segment_samples[-1].latitude), radian_to_degree(segment_samples[-1].longitude))).meters

                    sample.distance = segment_samples[-1].distance + distance_delta
                    if time_delta > timedelta(0):
                        sample.speed = distance_delta / time_delta.total_seconds()
                    else:
                        sample.speed = 0

                    # Option: Pause detection based on time delta threshold
                    if GPX_IMPORT_OPTION_PAUSE_DETECTION in import_options and time_delta > import_options[GPX_IMPORT_OPTION_PAUSE_DETECTION]:
                        pause_detected = True
                        sample.distance = segment_samples[-1].distance
                        sample.speed = 0

                # Track segment -> Track (multiple track segments contained)
                elif len(track_samples) > 0:
                    sample.time = track_samples[-1].time + (sample.utc - track_samples[-1].utc)  # Time diff to last sample of the previous track segment
                    sample.distance = track_samples[-1].distance
                    sample.speed = 0
                # Track -> Full GPX (multiple tracks contained)
                elif len(all_samples) > 0:
                    sample.time = all_samples[-1].time + (sample.utc - all_samples[-1].utc)  # Time diff to last sample of the previous track
                    sample.distance = all_samples[-1].distance
                    sample.speed = 0
                # First sample
                else:
                    sample.time = timedelta(0)
                    sample.distance = 0
                    sample.speed = 0

                parse_sample_extensions(sample, track_point)
                segment_samples.append(sample)

                # Finally insert a found pause based on time delta threshold
                if pause_detected:
                    insert_pause(segment_samples, len(segment_samples) - 1, move, pause_type=GPX_IMPORT_PAUSE_TYPE_PAUSE_DETECTION)
            # end for track_points

            # Insert an pause event between every track segment
            insert_pause_idx = len(track_samples)
            track_samples.extend(segment_samples)
            insert_pause(track_samples, insert_pause_idx, move, pause_type=GPX_TRKSEG)
        # end for track_segments

        # Insert an pause event between every track
        insert_pause_idx = len(all_samples)
        all_samples.extend(track_samples)
        insert_pause(all_samples, insert_pause_idx, move, pause_type=GPX_TRK)
    # end for tracks
    return all_samples


def insert_pause(samples, insert_pause_idx, move, pause_type):
    if (insert_pause_idx <= 0):
        return

    stop_sample = samples[insert_pause_idx - 1]
    start_sample = samples[insert_pause_idx]

    pause_duration = start_sample.time - stop_sample.time
    pause_distance = distance((radian_to_degree(stop_sample.latitude), radian_to_degree(stop_sample.longitude)),
                              (radian_to_degree(start_sample.latitude), radian_to_degree(start_sample.longitude))).meters

    # Introduce start of pause sample
    pause_sample = Sample()
    pause_sample.move = move
    pause_sample.utc = stop_sample.utc
    pause_sample.time = stop_sample.time
    stop_sample.utc -= timedelta(microseconds=1)  # Cut off 1ms from last recorded sample in order to introduce the new pause sample and keep time order
    stop_sample.time -= timedelta(microseconds=1)

    pause_sample.events = {"pause": {"state": "True",
                                     "type": str(pause_type),
                                     "duration": str(pause_duration),
                                     "distance": str(pause_distance),
                                    }}
    samples.insert(insert_pause_idx, pause_sample)  # Duplicate last element

    # Introduce end of pause sample
    pause_sample = Sample()
    pause_sample.move = move
    pause_sample.utc = start_sample.utc
    pause_sample.time = start_sample.time
    start_sample.utc += timedelta(microseconds=1)  # Add 1ms to the first recorded sample in order to introduce the new pause sample and keep time order
    start_sample.time += timedelta(microseconds=1)
    pause_sample.events = {"pause": {"state": "False",
                                     "duration": "0",
                                     "distance": "0",
                                     "type": str(pause_type)
                                    }}
    samples.insert(insert_pause_idx + 1, pause_sample)


def is_start_pause_sample(sample):
    return sample.events and "pause" in sample.events and sample.events["pause"]["state"].lower() == "true"


def create_move():
    move = Move()
    move.activity = GPX_ACTIVITY_TYPE
    move.import_date_time = datetime.now()
    return move


def create_device():
    device = Device()
    device.name = GPX_DEVICE_NAME
    device.serial_number = GPX_DEVICE_SERIAL
    return device


def derive_move_infos_from_samples(move, samples):
    if len(samples) <= 0:
        return

    move.date_time = samples[0].utc
    move.log_item_count = len(samples)

    move.duration = timedelta(0)  # Accumulated later without pauses

    speeds = np.asarray([sample.speed for sample in samples if sample.speed is not None], dtype=float)
    if len(speeds) > 0:
        move.speed_max = np.max(speeds)

    # Altitudes
    altitudes = np.asarray([sample.altitude for sample in samples if sample.altitude is not None], dtype=float)
    if len(altitudes) > 0:
        move.altitude_min = np.min(altitudes)
        move.altitude_max = np.max(altitudes)

        # Total ascent / descent
        move.ascent = 0
        move.ascent_time = timedelta(0)
        move.descent = 0
        move.descent_time = timedelta(0)

    previous_sample = None

    # Accumulate values from samples
    for sample in samples:
        # Skip calculation on first sample, sample without altitude info, pause event
        if previous_sample and not is_start_pause_sample(previous_sample):

            # Calculate altitude and time sums
            if sample.altitude is not None and previous_sample.altitude is not None:
                altitude_diff = sample.altitude - previous_sample.altitude
                time_diff = sample.time - previous_sample.time
                if altitude_diff > 0:
                    move.ascent += altitude_diff
                    move.ascent_time += time_diff
                elif altitude_diff < 0:
                    move.descent += -altitude_diff
                    move.descent_time += time_diff

            # Total duration
            move.duration += sample.utc - previous_sample.utc

        # Store sample for next cycle
        previous_sample = sample

    # Total Speed / Distance
    move.distance = samples[-1].distance
    if move.duration and move.duration > timedelta(0):
        move.speed_avg = move.distance / move.duration.total_seconds()

    # Temperature
    temperatures = np.asarray([sample.temperature for sample in samples if sample.temperature], dtype=float)
    if len(temperatures) > 0:
        move.temperature_min = np.min(temperatures)
        move.temperature_max = np.max(temperatures)

    # Heart rate
    hrs = np.asarray([sample.hr for sample in samples if sample.hr], dtype=float)
    if len(hrs) > 0:
        move.hr_min = np.min(hrs)
        move.hr_max = np.max(hrs)
        move.hr_avg = np.mean(hrs)


def get_gpx_import_options(request_form):
    import_options = { }
    if GPX_IMPORT_OPTION_PAUSE_DETECTION  in request_form:
        if request_form[GPX_IMPORT_OPTION_PAUSE_DETECTION] == 'on' and GPX_IMPORT_OPTION_PAUSE_DETECTION_THRESHOLD in request_form:
            try:
                import_options[GPX_IMPORT_OPTION_PAUSE_DETECTION] = timedelta(seconds=int(request_form[GPX_IMPORT_OPTION_PAUSE_DETECTION_THRESHOLD]))
            except:
                flash("Unsupported GPX import option 'pause detection' threshold value: '%s'" % request_form['gpx_option_pause_detection_threshold'])
                return None
    return import_options


def gpx_import(xmlfile, user, request_form):
    # Get users options
    import_options = get_gpx_import_options(request_form)
    if import_options == None:
        return

    filename = xmlfile.filename
    try:
        tree = objectify.parse(xmlfile).getroot()
    except Exception as e:
        flash("Failed to parse the GPX file! %s" % e.msg)
        return

    for namespace in GPX_NAMESPACES.values():
        if tree.tag.startswith(namespace):
            gpx_namespace = namespace
            break
    else:
        flash("Unsupported GPX format version: %s" % tree.tag)
        return

    device = create_device()
    persistent_device = Device.query.filter_by(serial_number=device.serial_number).scalar()
    if persistent_device:
        if not persistent_device.name:
            flash("update device name to '%s'" % device.name)
            persistent_device.name = device.name
        else:
            assert device.name == persistent_device.name
        device = persistent_device
    else:
        db.session.add(device)

    move = create_move()
    move.source = os.path.abspath(filename)
    move.import_module = __name__

    # Parse samples
    all_samples = parse_samples(tree, move, gpx_namespace, import_options)

    derive_move_infos_from_samples(move, all_samples)

    if Move.query.filter_by(user=user, date_time=move.date_time, device=device).scalar():
        flash("%s at %s already exists" % (move.activity, move.date_time), 'warning')
    else:
        move.user = user
        move.device = device
        db.session.add(move)

        for sample in all_samples:
            db.session.add(sample)

        postprocess_move(move)

        db.session.commit()
        return move
