# Copyright 2019-2020, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Parsing for Bandwidth Authority metrics as described in Tor's
`bandwidth-file-spec <https://gitweb.torproject.org/torspec.git/tree/bandwidth-file-spec.txt>`_.

**Module Overview:**

::

  BandwidthFile - Tor bandwidth authority measurements.

.. versionadded:: 1.8.0
"""

import collections
import datetime
import io
import time

import stem.util.str_tools
import stem.version

from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Type

from stem.descriptor import (
  ENTRY_TYPE,
  _mappings_for,
  Descriptor,
)

# Four character dividers are allowed for backward compatability, but five is
# preferred.

HEADER_DIV = b'====='
HEADER_DIV_ALT = b'===='


class RecentStats(object):
  """
  Statistical information collected over the last 'data_period' (by default
  five days).

  :var int consensus_count: number of consensuses published during this period

  :var int prioritized_relays: number of relays prioritized to be measured
  :var int prioritized_relay_lists: number of times a set of relays were
    prioritized to be measured

  :var int measurement_attempts: number of relay measurements we attempted
  :var int measurement_failures: number of measurement attempts that failed

  :var RelayFailures relay_failures: number of relays we failed to measure
  """

  def __init__(self) -> None:
    self.consensus_count = None
    self.prioritized_relays = None
    self.prioritized_relay_lists = None
    self.measurement_attempts = None
    self.measurement_failures = None
    self.relay_failures = RelayFailures()


class RelayFailures(object):
  """
  Summary of the number of relays we were unable to measure.

  :var int no_measurement: number of relays that did not have any successful
    measurements
  :var int insuffient_period: number of relays whos measurements were collected
    over a period that was too small (1 day by default)
  :var int insufficient_measurements: number of relays we did not collect
    enough measurements for (2 by default)
  :var int stale: number of relays whos latest measurement is too old (5 days
    by default)
  """

  def __init__(self) -> None:
    self.no_measurement = None
    self.insuffient_period = None
    self.insufficient_measurements = None
    self.stale = None


# Converts header attributes to a given type. Malformed fields should be
# ignored according to the spec.

def _str(val: str) -> str:
  return val  # already a str


def _int(val: str) -> int:
  return int(val) if (val and val.isdigit()) else None


def _date(val: str) -> datetime.datetime:
  try:
    return stem.util.str_tools._parse_iso_timestamp(val)
  except ValueError:
    return None  # not an iso formatted date


def _csv(val: str) -> Sequence[str]:
  return list(map(lambda v: v.strip(), val.split(','))) if val is not None else None


def _tor_version(val: str) -> stem.version.Version:
  try:
    return stem.version.Version(val) if val else None
  except ValueError:
    return None  # invalid tor version


# mapping of attributes => (header, type)

HEADER_ATTR = {
  # version 1.1.0 introduced headers

  'version': ('version', _str),

  'software': ('software', _str),
  'software_version': ('software_version', _str),

  'earliest_bandwidth': ('earliest_bandwidth', _date),
  'latest_bandwidth': ('latest_bandwidth', _date),
  'created_at': ('file_created', _date),
  'generated_at': ('generator_started', _date),

  # version 1.2.0 additions

  'consensus_size': ('number_consensus_relays', _int),
  'eligible_count': ('number_eligible_relays', _int),
  'eligible_percent': ('percent_eligible_relays', _int),
  'min_count': ('minimum_number_eligible_relays', _int),
  'min_percent': ('minimum_percent_eligible_relays', _int),

  # version 1.3.0 additions

  'scanner_country': ('scanner_country', _str),
  'destinations_countries': ('destinations_countries', _csv),

  # version 1.4.0 additions

  'time_to_report_half_network': ('time_to_report_half_network', _int),

  'recent_stats.consensus_count': ('recent_consensus_count', _int),
  'recent_stats.prioritized_relay_lists': ('recent_priority_list_count', _int),
  'recent_stats.prioritized_relays': ('recent_priority_relay_count', _int),
  'recent_stats.measurement_attempts': ('recent_measurement_attempt_count', _int),
  'recent_stats.measurement_failures': ('recent_measurement_failure_count', _int),
  'recent_stats.relay_failures.no_measurement': ('recent_measurements_excluded_error_count', _int),
  'recent_stats.relay_failures.insuffient_period': ('recent_measurements_excluded_near_count', _int),
  'recent_stats.relay_failures.insufficient_measurements': ('recent_measurements_excluded_few_count', _int),
  'recent_stats.relay_failures.stale': ('recent_measurements_excluded_old_count', _int),
  'tor_version': ('tor_version', _tor_version),
}

HEADER_DEFAULT = {
  'version': '1.0.0',  # version field was added in 1.1.0
}


def _parse_file(descriptor_file: BinaryIO, validate: bool = False, **kwargs: Any) -> Iterator['stem.descriptor.bandwidth_file.BandwidthFile']:
  """
  Iterates over the bandwidth authority metrics in a file.

  :param descriptor_file: file with descriptor content
  :param validate: checks the validity of the descriptor's content if
    **True**, skips these checks otherwise
  :param kwargs: additional arguments for the descriptor constructor

  :returns: :class:`stem.descriptor.bandwidth_file.BandwidthFile` object

  :raises:
    * **ValueError** if the contents is malformed and validate is **True**
    * **OSError** if the file can't be read
  """

  if kwargs:
    raise ValueError('BUG: keyword arguments unused by bandwidth files')

  yield BandwidthFile(descriptor_file.read(), validate)


def _parse_header(descriptor: 'stem.descriptor.Descriptor', entries: ENTRY_TYPE) -> None:
  header = collections.OrderedDict()  # type: collections.OrderedDict[str, str]
  content = io.BytesIO(descriptor.get_bytes())

  content.readline()  # skip the first line, which should be the timestamp

  index = 1
  version_index = None

  while True:
    line = content.readline().strip()

    if (not line or line in (HEADER_DIV, HEADER_DIV_ALT)
        or not header and b'node_id=' in line):
      break  # end of the content
    if b'=' not in line:
      raise ValueError(
          f"Header expected to be key=value pairs, but had '{stem.util.str_tools._to_unicode(line)}'"
      )

    key, value = stem.util.str_tools._to_unicode(line).split('=', 1)
    header[key] = value

    if key == 'version':
      version_index = index
    index += 1

  descriptor.header = header
  descriptor.recent_stats = RecentStats()

  for full_attr, (keyword, cls) in HEADER_ATTR.items():
    obj = descriptor

    for attr in full_attr.split('.')[:-1]:
      obj = getattr(obj, attr)

    setattr(obj, full_attr.split('.')[-1], cls(header.get(keyword, HEADER_DEFAULT.get(full_attr))))

  if version_index is not None and version_index != 1:
    raise ValueError("The 'version' header must be in the second position")


def _parse_timestamp(descriptor: 'stem.descriptor.Descriptor', entries: ENTRY_TYPE) -> None:
  first_line = io.BytesIO(descriptor.get_bytes()).readline().strip()

  if first_line.isdigit():
    descriptor.timestamp = datetime.datetime.utcfromtimestamp(int(first_line))
  else:
    raise ValueError(
        f"First line should be a unix timestamp, but was '{stem.util.str_tools._to_unicode(first_line)}'"
    )


def _parse_body(descriptor: 'stem.descriptor.Descriptor', entries: ENTRY_TYPE) -> None:
  # In version 1.0.0 the body is everything after the first line. Otherwise
  # it's everything after the header's divider.

  content = io.BytesIO(descriptor.get_bytes())

  if descriptor.version == '1.0.0':
    content.readline()  # skip the first line
  else:
    while content.readline().strip() not in ('', HEADER_DIV, HEADER_DIV_ALT):
      pass  # skip the header

  measurements = {}

  for line_bytes in content.readlines():
    line = stem.util.str_tools._to_unicode(line_bytes.strip())
    attr = dict(_mappings_for('measurement', line))
    fingerprint = attr.get('node_id', '').lstrip('$')  # bwauths prefix fingerprints with '$'

    if not fingerprint:
      raise ValueError(
          f"Every meaurement must include 'node_id': {stem.util.str_tools._to_unicode(line)}"
      )
    elif fingerprint in measurements:
      raise ValueError(
          f'Relay {fingerprint} is listed multiple times. It should only be present once.'
      )

    measurements[fingerprint] = attr

  descriptor.measurements = measurements


class BandwidthFile(Descriptor):
  """
  Tor bandwidth authority measurements.

  :var dict measurements: **\\*** mapping of relay fingerprints to their
    bandwidth measurement metadata

  :var dict header: **\\*** header metadata
  :var datetime timestamp: **\\*** time when these metrics were published
  :var str version: **\\*** document format version

  :var str software: application that generated these metrics
  :var str software_version: version of the application that generated these metrics

  :var datetime earliest_bandwidth: time of the first sampling
  :var datetime latest_bandwidth: time of the last sampling
  :var datetime created_at: time when this file was created
  :var datetime generated_at: time when collection of these metrics started

  :var int consensus_size: number of relays in the consensus
  :var int eligible_count: relays with enough measurements to be included
  :var int eligible_percent: percentage of consensus with enough measurements
  :var int min_count: minimum eligible relays for results to be provided
  :var int min_percent: minimum measured percentage of the consensus

  :var str scanner_country: country code where this scan took place
  :var list destinations_countries: all country codes that were scanned
  :var stem.version.Version tor_version: scanner's tor version

  :var int time_to_report_half_network: estimated number of seconds required to
    measure half the network, given recent measurements

  :var RecentStats recent_stats: statistical information collected over the
    last 'data_period' (by default five days)

  **\\*** attribute is either required when we're parsed with validation or has
  a default value, others are left as **None** if undefined
  """

  TYPE_ANNOTATION_NAME = 'bandwidth-file'

  ATTRIBUTES = {
    'timestamp': (None, _parse_timestamp),
    'header': ({}, _parse_header),
    'measurements': ({}, _parse_body),
  }  # type: Dict[str, Tuple[Any, Callable[['stem.descriptor.Descriptor', ENTRY_TYPE], None]]]

  ATTRIBUTES.update(dict([(k, (None, _parse_header)) for k in HEADER_ATTR.keys()]))

  @classmethod
  def content(cls: Type['stem.descriptor.bandwidth_file.BandwidthFile'], attr: Optional[Mapping[str, str]] = None, exclude: Sequence[str] = ()) -> bytes:
    """
    Creates descriptor content with the given attributes. This descriptor type
    differs somewhat from others and treats our attr/exclude attributes as
    follows...

      * 'timestamp' is a reserved key for our mandatory header unix timestamp.

      * 'content' is a reserved key for our bandwidth measurement lines.

      * All other keys are treated as header fields.

    For example...

    ::

      BandwidthFile.content({
        'timestamp': '12345',
        'version': '1.2.0',
        'content': [],
      })
    """

    header = collections.OrderedDict(attr) if attr is not None else collections.OrderedDict()
    timestamp = header.pop('timestamp', str(int(time.time())))
    content = header.pop('content', [])  # type: List[str] # type: ignore
    version = header.get('version', HEADER_DEFAULT.get('version'))

    lines = []

    if 'timestamp' not in exclude:
      lines.append(stem.util.str_tools._to_bytes(timestamp))

    if version == '1.0.0' and header:
      raise ValueError('Headers require BandwidthFile version 1.1 or later')
    elif version != '1.0.0':
      # ensure 'version' is the second header

      if 'version' not in exclude:
        lines.append(stem.util.str_tools._to_bytes(f"version={header.pop('version')}"))

      lines.extend(
          stem.util.str_tools._to_bytes(f'{k}={v}') for k, v in header.items())
      lines.append(HEADER_DIV)

    lines.extend(
        stem.util.str_tools._to_bytes(measurement) for measurement in content)
    return b'\n'.join(lines)

  def __init__(self, raw_content: bytes, validate: bool = False) -> None:
    super(BandwidthFile, self).__init__(raw_content, lazy_load = not validate)

    if validate:
      _parse_timestamp(self, None)
      _parse_header(self, None)
      _parse_body(self, None)
