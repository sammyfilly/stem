# Copyright 2013-2020, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Microdescriptors are a distilled copy of a relay's server descriptor,
downloaded by Tor clients in lieu of server descriptors to reduce
bandwidth usage.

Microdescriptors contain a subset of a server descriptor's information
and are rather clunky to use. For instance, microdescriptors lack a
relay's canonical identifier (its fingerprint). To get it you must
pair microdescriptors with the consensus from which they came...

::

  import os

  from stem.control import Controller
  from stem.descriptor import parse_file

  with Controller.from_port() as controller:
    controller.authenticate()

    exit_digests = set()
    data_dir = controller.get_conf('DataDirectory')

    for desc in controller.get_microdescriptors():
      if desc.exit_policy.is_exiting_allowed():
        exit_digests.add(desc.digest())

    print('Exit Relays:')

    for desc in parse_file(os.path.join(data_dir, 'cached-microdesc-consensus')):
      if desc.microdescriptor_digest in exit_digests:
        print('  %s (%s)' % (desc.nickname, desc.fingerprint))

Doing the same is trivial with server descriptors...

::

  from stem.control import Controller

  with Controller.from_port() as controller:
    controller.authenticate()

    for desc in controller.get_server_descriptors():
      if desc.exit_policy.is_exiting_allowed():
        print('  %s (%s)' % (desc.nickname, desc.fingerprint))

**Module Overview:**

::

  Microdescriptor - Tor microdescriptor.
"""

import functools
import hashlib

import stem.exit_policy

from typing import Any, BinaryIO, Dict, Iterator, Mapping, Optional, Sequence, Type, Union

from stem.descriptor import (
  ENTRY_TYPE,
  Descriptor,
  DigestHash,
  DigestEncoding,
  _descriptor_content,
  _descriptor_components,
  _read_until_keywords,
  _values,
  _parse_simple_line,
  _parse_protocol_line,
  _parse_key_block,
  _random_crypto_blob,
)

from stem.descriptor.router_status_entry import (
  _parse_a_line,
  _parse_p_line,
)

REQUIRED_FIELDS = (
  'onion-key',
)

SINGLE_FIELDS = (
  'onion-key',
  'ntor-onion-key',
  'family',
  'p',
  'p6',
  'pr',
)


def _parse_file(descriptor_file: BinaryIO, validate: bool = False, **kwargs: Any) -> Iterator['stem.descriptor.microdescriptor.Microdescriptor']:
  """
  Iterates over the microdescriptors in a file.

  :param descriptor_file: file with descriptor content
  :param validate: checks the validity of the descriptor's content if
    **True**, skips these checks otherwise
  :param kwargs: additional arguments for the descriptor constructor

  :returns: iterator for Microdescriptor instances in the file

  :raises:
    * **ValueError** if the contents is malformed and validate is True
    * **OSError** if the file can't be read
  """

  if kwargs:
    raise ValueError('BUG: keyword arguments unused by microdescriptors')

  while True:
    annotations = _read_until_keywords('onion-key', descriptor_file)

    # read until we reach an annotation or onion-key line
    descriptor_lines = []

    if onion_key_line := descriptor_file.readline():
      descriptor_lines.append(onion_key_line)
    else:
      break

    while True:
      last_position = descriptor_file.tell()
      line = descriptor_file.readline()

      if not line:
        break  # EOF
      elif line.startswith(b'@') or line.startswith(b'onion-key'):
        descriptor_file.seek(last_position)
        break
      else:
        descriptor_lines.append(line)

    if descriptor_lines:
      if descriptor_lines[0].startswith(b'@type'):
        descriptor_lines = descriptor_lines[1:]

      # strip newlines from annotations
      annotations = list(map(bytes.strip, annotations))

      descriptor_text = bytes.join(b'', descriptor_lines)

      yield Microdescriptor(descriptor_text, validate, annotations)
    else:
      break  # done parsing descriptors


def _parse_id_line(descriptor: 'stem.descriptor.Descriptor', entries: ENTRY_TYPE) -> None:
  identities = {}

  for entry in _values('id', entries):
    entry_comp = entry.split()

    if len(entry_comp) < 2:
      raise ValueError(
          f"'id' lines should contain both the key type and digest: id {entry}"
      )

    key_type, key_value = entry_comp[0], entry_comp[1]

    if key_type in identities:
      raise ValueError(
          f"There can only be one 'id' line per a key type, but '{key_type}' appeared multiple times"
      )

    identities[key_type] = key_value
  descriptor.identifiers = identities


_parse_onion_key_line = _parse_key_block('onion-key', 'onion_key', 'RSA PUBLIC KEY')
_parse_ntor_onion_key_line = _parse_simple_line('ntor-onion-key', 'ntor_onion_key')
_parse_family_line = _parse_simple_line('family', 'family', func = lambda v: v.split(' '))
_parse_p6_line = _parse_simple_line('p6', 'exit_policy_v6', func = lambda v: stem.exit_policy.MicroExitPolicy(v))
_parse_pr_line = _parse_protocol_line('pr', 'protocols')


class Microdescriptor(Descriptor):
  """
  Microdescriptor (`descriptor specification
  <https://gitweb.torproject.org/torspec.git/tree/dir-spec.txt>`_)

  :var str onion_key: **\\*** key used to encrypt EXTEND cells
  :var str ntor_onion_key: base64 key used to encrypt EXTEND in the ntor protocol
  :var list or_addresses: **\\*** alternative for our address/or_port attributes, each
    entry is a tuple of the form (address (**str**), port (**int**), is_ipv6
    (**bool**))
  :var list family: **\\*** nicknames or fingerprints of declared family
  :var stem.exit_policy.MicroExitPolicy exit_policy: **\\*** relay's exit policy
  :var stem.exit_policy.MicroExitPolicy exit_policy_v6: **\\*** exit policy for IPv6
  :var hash identifiers: mapping of key types (like rsa1024 or ed25519) to
    their base64 encoded identity, this is only used for collision prevention
    (:ticket:`tor-11743`)
  :var dict protocols: mapping of protocols to their supported versions

  **\\*** attribute is required when we're parsed with validation

  .. versionchanged:: 1.5.0
     Added the identifiers attribute.

  .. versionchanged:: 1.6.0
     Added the protocols attribute.

  .. versionchanged:: 1.8.0
     Replaced our **digest** attribute with a much more flexible **digest()**
     method. Unfortunately I cannot do this in a backward compatible way
     because of the name conflict. The old digest had multiple problems (for
     instance, being hex rather than base64 encoded), so hopefully no one was
     using it. Very sorry if this causes trouble for anyone.
  """

  TYPE_ANNOTATION_NAME = 'microdescriptor'

  ATTRIBUTES = {
    'onion_key': (None, _parse_onion_key_line),
    'ntor_onion_key': (None, _parse_ntor_onion_key_line),
    'or_addresses': ([], _parse_a_line),
    'family': ([], _parse_family_line),
    'exit_policy': (stem.exit_policy.MicroExitPolicy('reject 1-65535'), _parse_p_line),
    'exit_policy_v6': (None, _parse_p6_line),
    'identifiers': ({}, _parse_id_line),
    'protocols': ({}, _parse_pr_line),
  }

  PARSER_FOR_LINE = {
    'onion-key': _parse_onion_key_line,
    'ntor-onion-key': _parse_ntor_onion_key_line,
    'a': _parse_a_line,
    'family': _parse_family_line,
    'p': _parse_p_line,
    'p6': _parse_p6_line,
    'pr': _parse_pr_line,
    'id': _parse_id_line,
  }

  @classmethod
  def content(cls: Type['stem.descriptor.microdescriptor.Microdescriptor'], attr: Optional[Mapping[str, str]] = None, exclude: Sequence[str] = ()) -> bytes:
    return _descriptor_content(attr, exclude, (
      ('onion-key', _random_crypto_blob('RSA PUBLIC KEY')),
    ))

  def __init__(self, raw_contents: bytes, validate: bool = False, annotations: Optional[Sequence[bytes]] = None) -> None:
    super(Microdescriptor, self).__init__(raw_contents, lazy_load = not validate)
    self._annotation_lines = annotations if annotations else []
    entries = _descriptor_components(raw_contents, validate)

    if validate:
      self._parse(entries, validate)
      self._check_constraints(entries)
    else:
      self._entries = entries

  def digest(self, hash_type: 'stem.descriptor.DigestHash' = DigestHash.SHA256, encoding: 'stem.descriptor.DigestEncoding' = DigestEncoding.BASE64) -> Union[str, 'hashlib._HASH']:  # type: ignore
    """
    Digest of this microdescriptor. These are referenced by...

      * **Microdescriptor Consensus**

        * Referer: :class:`~stem.descriptor.router_status_entry.RouterStatusEntryMicroV3` **digest** attribute
        * Format: **SHA256/BASE64**

    .. versionadded:: 1.8.0

    :param hash_type: digest hashing algorithm
    :param encoding: digest encoding

    :returns: **hashlib.HASH** or **str** based on our encoding argument
    """

    if hash_type == DigestHash.SHA1:
      return stem.descriptor._encode_digest(hashlib.sha1(self.get_bytes()), encoding)
    elif hash_type == DigestHash.SHA256:
      return stem.descriptor._encode_digest(hashlib.sha256(self.get_bytes()), encoding)
    else:
      raise NotImplementedError(
          f'Microdescriptor digests are only available in sha1 and sha256, not {hash_type}'
      )

  @functools.lru_cache()
  def get_annotations(self) -> Dict[bytes, bytes]:
    """
    Provides content that appeared prior to the descriptor. If this comes from
    the cached-microdescs then this commonly contains content like...

    ::

      @last-listed 2013-02-24 00:18:30

    :returns: **dict** with the key/value pairs in our annotations
    """

    annotation_dict = {}

    for line in self._annotation_lines:
      if b' ' in line:
        key, value = line.split(b' ', 1)
        annotation_dict[key] = value
      else:
        annotation_dict[line] = None

    return annotation_dict

  def get_annotation_lines(self) -> Sequence[bytes]:
    """
    Provides the lines of content that appeared prior to the descriptor. This
    is the same as the
    :func:`~stem.descriptor.microdescriptor.Microdescriptor.get_annotations`
    results, but with the unparsed lines and ordering retained.

    :returns: **list** with the lines of annotation that came before this descriptor
    """

    return self._annotation_lines

  def _check_constraints(self, entries: ENTRY_TYPE) -> None:
    """
    Does a basic check that the entries conform to this descriptor type's
    constraints.

    :param entries: keyword => (value, pgp key) entries

    :raises: **ValueError** if an issue arises in validation
    """

    for keyword in REQUIRED_FIELDS:
      if keyword not in entries:
        raise ValueError(f"Microdescriptor must have a '{keyword}' entry")

    for keyword in SINGLE_FIELDS:
      if keyword in entries and len(entries[keyword]) > 1:
        raise ValueError(
            f"The '{keyword}' entry can only appear once in a microdescriptor")

    if list(entries.keys())[0] != 'onion-key':
      raise ValueError("Microdescriptor must start with a 'onion-key' entry")

  def _name(self, is_plural: bool = False) -> str:
    return 'microdescriptors' if is_plural else 'microdescriptor'
