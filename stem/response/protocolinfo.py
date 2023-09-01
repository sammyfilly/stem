# Copyright 2012-2020, Damian Johnson and The Tor Project
# See LICENSE for licensing information

import sys

import stem.response
import stem.socket
import stem.version
import stem.util.str_tools

from stem.util import log
from typing import Tuple


class ProtocolInfoResponse(stem.response.ControlMessage):
  """
  Version one PROTOCOLINFO query response.

  The protocol_version is the only mandatory data for a valid PROTOCOLINFO
  response, so all other values are None if undefined or empty if a collection.

  :var int protocol_version: protocol version of the response
  :var stem.version.Version tor_version: version of the tor process
  :var tuple auth_methods: :data:`stem.connection.AuthMethod` types that tor will accept
  :var tuple unknown_auth_methods: strings of unrecognized auth methods
  :var str cookie_path: path of tor's authentication cookie
  """

  def _parse_message(self) -> None:
    # Example:
    #   250-PROTOCOLINFO 1
    #   250-AUTH METHODS=COOKIE COOKIEFILE="/home/atagar/.tor/control_auth_cookie"
    #   250-VERSION Tor="0.2.1.30"
    #   250 OK

    from stem.connection import AuthMethod

    self.protocol_version = None
    self.tor_version = None
    self.auth_methods = ()  # type: Tuple[stem.connection.AuthMethod, ...]
    self.unknown_auth_methods = ()  # type: Tuple[str, ...]
    self.cookie_path = None

    auth_methods, unknown_auth_methods = [], []
    remaining_lines = list(self)

    if not self.is_ok() or remaining_lines.pop() != 'OK':
      raise stem.ProtocolError("PROTOCOLINFO response didn't have an OK status:\n%s" % self)

    # sanity check that we're a PROTOCOLINFO response
    if not remaining_lines[0].startswith('PROTOCOLINFO'):
      raise stem.ProtocolError('Message is not a PROTOCOLINFO response:\n%s' % self)

    while remaining_lines:
      line = remaining_lines.pop(0)
      line_type = line.pop()

      if line_type == 'PROTOCOLINFO':
        # Line format:
        #   FirstLine = "PROTOCOLINFO" SP PIVERSION CRLF
        #   PIVERSION = 1*DIGIT

        if line.is_empty():
          raise stem.ProtocolError(
              f"PROTOCOLINFO response's initial line is missing the protocol version: {line}"
          )

        try:
          self.protocol_version = int(line.pop())
        except ValueError:
          raise stem.ProtocolError(
              f'PROTOCOLINFO response version is non-numeric: {line}')

        # The piversion really should be '1' but, according to the spec, tor
        # does not necessarily need to provide the PROTOCOLINFO version that we
        # requested. Log if it's something we aren't expecting but still make
        # an effort to parse like a v1 response.

        if self.protocol_version != 1:
          log.info("We made a PROTOCOLINFO version 1 query but got a version %i response instead. We'll still try to use it, but this may cause problems." % self.protocol_version)
      elif line_type == 'AUTH':
        # Line format:
        #   AuthLine = "250-AUTH" SP "METHODS=" AuthMethod *("," AuthMethod)
        #              *(SP "COOKIEFILE=" AuthCookieFile) CRLF
        #   AuthMethod = "NULL" / "HASHEDPASSWORD" / "COOKIE"
        #   AuthCookieFile = QuotedString

        # parse AuthMethod mapping
        if not line.is_next_mapping('METHODS'):
          raise stem.ProtocolError(
              f"PROTOCOLINFO response's AUTH line is missing its mandatory 'METHODS' mapping: {line}"
          )

        for method in line.pop_mapping()[1].split(','):
          if method == 'COOKIE':
            auth_methods.append(AuthMethod.COOKIE)
          elif method == 'HASHEDPASSWORD':
            auth_methods.append(AuthMethod.PASSWORD)
          elif method == 'NULL':
            auth_methods.append(AuthMethod.NONE)
          elif method == 'SAFECOOKIE':
            auth_methods.append(AuthMethod.SAFECOOKIE)
          else:
            unknown_auth_methods.append(method)
            message_id = f'stem.response.protocolinfo.unknown_auth_{method}'
            log.log_once(
                message_id,
                log.INFO,
                f"PROTOCOLINFO response included a type of authentication that we don't recognize: {method}",
            )

            # our auth_methods should have a single AuthMethod.UNKNOWN entry if
            # any unknown authentication methods exist
            if AuthMethod.UNKNOWN not in auth_methods:
              auth_methods.append(AuthMethod.UNKNOWN)

        # parse optional COOKIEFILE mapping (quoted and can have escapes)

        if line.is_next_mapping('COOKIEFILE', True, True):
          path = line._pop_mapping_bytes(True, True)[1]

          # fall back if our filesystem encoding doesn't work

          for encoding in [sys.getfilesystemencoding(), 'utf-8', 'latin-1']:
            try:
              self.cookie_path = path.decode(encoding)
              break
            except ValueError:
              pass

          if self.cookie_path is None:
            raise stem.ProtocolError(
                f"Cookie path '{repr(path)}' mismatches our filesystem encoding ({sys.getfilesystemencoding()})"
            )
      elif line_type == 'VERSION':
        # Line format:
        #   VersionLine = "250-VERSION" SP "Tor=" TorVersion OptArguments CRLF
        #   TorVersion = QuotedString

        if not line.is_next_mapping('Tor', True):
          raise stem.ProtocolError(
              f"PROTOCOLINFO response's VERSION line is missing its mandatory tor version mapping: {line}"
          )

        try:
          self.tor_version = stem.version.Version(line.pop_mapping(True)[1])
        except ValueError as exc:
          raise stem.ProtocolError(exc)
      else:
        log.debug(
            f"Unrecognized PROTOCOLINFO line type '{line_type}', ignoring it: {line}"
        )

    self.auth_methods = tuple(auth_methods)
    self.unknown_auth_methods = tuple(unknown_auth_methods)
