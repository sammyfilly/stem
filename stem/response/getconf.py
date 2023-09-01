# Copyright 2012-2020, Damian Johnson and The Tor Project
# See LICENSE for licensing information

import stem.response
import stem.socket

from typing import Dict, List


class GetConfResponse(stem.response.ControlMessage):
  """
  Reply for a GETCONF query.

  Note that configuration parameters won't match what we queried for if it's one
  of the special mapping options (ex. 'HiddenServiceOptions').

  :var dict entries: mapping between the config parameter (**str**) and their
    values (**list** of **str**)
  """

  def _parse_message(self) -> None:
    # Example:
    # 250-CookieAuthentication=0
    # 250-ControlPort=9100
    # 250-DataDirectory=/home/neena/.tor
    # 250 DirPort

    self.entries = {}  # type: Dict[str, List[str]]
    remaining_lines = list(self)

    if self.content() == [('250', ' ', 'OK')]:
      return

    if not self.is_ok():
      if unrecognized_keywords := [
          line[32:-1] for code, _, line in self.content()
          if code == '552' and line.startswith(
              'Unrecognized configuration key "') and line.endswith('"')
      ]:
        raise stem.InvalidArguments(
            '552',
            f"GETCONF request contained unrecognized keywords: {', '.join(unrecognized_keywords)}",
            unrecognized_keywords,
        )
      else:
        raise stem.ProtocolError('GETCONF response contained a non-OK status code:\n%s' % self)

    while remaining_lines:
      line = remaining_lines.pop(0)

      if line.is_next_mapping():
        key, value = line.split('=', 1)
      else:
        key, value = (line.pop(), None)

      # Tor's CommaList and RouterList have a bug where they map to an empty
      # string when undefined rather than None...
      #
      # https://gitlab.torproject.org/tpo/core/tor/-/issues/18263

      if value == '':
        value = None

      if key not in self.entries:
        self.entries[key] = []

      if value is not None:
        self.entries[key].append(value)
