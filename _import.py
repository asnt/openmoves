# vim: set fileencoding=utf-8 :
import sqlalchemy
import re
from datetime import timedelta, datetime


def normalize_move(move):
    if move.ascent_time.total_seconds() == 0:
        assert move.ascent == 0
        move.ascent = None

    if move.descent_time.total_seconds() == 0:
        assert float(move.descent) == 0
        move.descent = None

    if move.activity == 'Outdoor swimmin':
        move.activity = 'Outdoor swimming'


def remove_namespace(tag, ns='http://www.suunto.com/schemas/sml'):
    if tag.startswith('{'):
        ns = "{%s}" % ns
        assert tag.startswith(ns), "illegal tag: '%s'" % tag
        return tag[len(ns):]
    else:
        return tag


normalized_tags_cache = {}


def normalize_tag(tag):
    if tag in normalized_tags_cache:
        return normalized_tags_cache[tag]

    normalized_tag = remove_namespace(tag)

    # http://stackoverflow.com/a/1176023/4308
    normalized_tag = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', normalized_tag)
    normalized_tag = re.sub('([a-z0-9])([A-Z])', r'\1_\2', normalized_tag)
    normalized_tag = normalized_tag.lower()

    normalized_tags_cache[tag] = normalized_tag
    return normalized_tag


def _convert_attr(column_type, value, attr, message):
    if column_type == sqlalchemy.sql.sqltypes.Float:
        return float(value)
    elif column_type == sqlalchemy.sql.sqltypes.Interval:
        seconds = float(value)
        return timedelta(seconds=seconds)
    elif column_type == sqlalchemy.sql.sqltypes.Integer:
        if value == '0':
            return 0
        else:
            if value.startswith('0x'):
                return int(value, 16)
            else:
                return int(value, 10)

    elif column_type == sqlalchemy.sql.sqltypes.String:
        return value
    elif column_type == sqlalchemy.sql.sqltypes.DateTime:
        date, time = value.split('T')
        year, month, day = date.split('-')
        if time[-1] == 'Z':
            time = time[:-1]
        hour, minute, seconds = time.split(':')
        seconds = float(seconds)
        second = int(seconds)
        microsecond = int((seconds - second) * (10 ** 6))
        datetime_value = datetime(year=int(year),
                                  month=int(month),
                                  day=int(day),
                                  hour=int(hour),
                                  minute=int(minute),
                                  second=second,
                                  microsecond=microsecond)

        return datetime_value
    else:
        raise Exception("Unknown column type: %s for attribute '%s'" % (column_type, attr))


def set_attr(move, attr, value):
    prop = getattr(type(move), attr).property
    assert len(prop.columns) == 1

    value = _convert_attr(type(prop.columns[0].type), value, attr, "illegal value '%s' for '%s'" % (value, attr))
    setattr(move, attr, value)


def add_children(move, element):
    for child in element.iterchildren():
        tag = normalize_tag(child.tag)

        if tag in ('speed', 'hr', 'cadence', 'temperature', 'altitude'):
            for sub_child in child.iterchildren():
                sub_tag = tag + '_' + normalize_tag(sub_child.tag)
                set_attr(move, sub_tag, value=sub_child.text)
        else:
            set_attr(move, tag, value=child.text)


def _parse_recursive(node):
    if node.countchildren() > 0:
        events = {}
        for child in node.iterchildren():
            tag = remove_namespace(child.tag)
            if len(tag) > 2 and tag.upper() != tag:
                tag = tag[0].lower() + tag[1:]

            data = _parse_recursive(child)
            if tag in events:
                if not isinstance(events[tag], list):
                    events[tag] = [events[tag]]
                events[tag].append(data)
            else:
                events[tag] = data
        return events
    else:
        if len(node.text) > 0:
            return node.text
        else:
            return None


def parse_json(node):
    return _parse_recursive(node)
