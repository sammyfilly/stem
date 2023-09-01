# Copyright 2015-2020, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Parsing for Tor hidden service descriptors as described in Tor's `version 2
<https://gitweb.torproject.org/torspec.git/tree/rend-spec-v2.txt>`_ and
`version 3 <https://gitweb.torproject.org/torspec.git/tree/rend-spec-v3.txt>`_
rend-spec.

Unlike other descriptor types these describe a hidden service rather than a
relay. They're created by the service, and can only be fetched via relays with
the HSDir flag.

These are only available through the Controller's
:func:`~stem.control.Controller.get_hidden_service_descriptor` method.

**Module Overview:**

::

  HiddenServiceDescriptor - Common parent for hidden service descriptors
    |- HiddenServiceDescriptorV2 - Version 2 hidden service descriptor
    +- HiddenServiceDescriptorV3 - Version 3 hidden service descriptor
         |- address_from_identity_key - convert an identity key to address
         |- identity_key_from_address - convert an address to identity key
         +- decrypt - decrypt and parse encrypted layers

  OuterLayer - First encrypted layer of a hidden service v3 descriptor
  InnerLayer - Second encrypted layer of a hidden service v3 descriptor

.. versionadded:: 1.4.0
"""

import base64
import binascii
import collections
import datetime
import functools
import hashlib
import io
import os
import struct
import time

import stem.client.datatype
import stem.descriptor.certificate
import stem.util
import stem.util.connection
import stem.util.str_tools
import stem.util.tor_tools

from stem.client.datatype import CertType
from stem.descriptor.certificate import ExtensionType, Ed25519Extension, Ed25519Certificate, Ed25519CertificateV1
from typing import Any, BinaryIO, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Type, Union

from stem.descriptor import (
  ENTRY_TYPE,
  PGP_BLOCK_END,
  Descriptor,
  _descriptor_content,
  _descriptor_components,
  _read_until_keywords,
  _bytes_for_block,
  _value,
  _values,
  _parse_simple_line,
  _parse_if_present,
  _parse_int_line,
  _parse_timestamp_line,
  _parse_key_block,
  _random_date,
  _random_crypto_blob,
)

try:
  from cryptography.hazmat.backends.openssl.backend import backend
  X25519_AVAILABLE = hasattr(backend, 'x25519_supported') and backend.x25519_supported()
except ImportError:
  X25519_AVAILABLE = False


REQUIRED_V2_FIELDS = (
  'rendezvous-service-descriptor',
  'version',
  'permanent-key',
  'secret-id-part',
  'publication-time',
  'protocol-versions',
  'signature',
)

REQUIRED_V3_FIELDS = (
  'hs-descriptor',
  'descriptor-lifetime',
  'descriptor-signing-key-cert',
  'revision-counter',
  'superencrypted',
  'signature',
)

INTRODUCTION_POINTS_ATTR = {
  'identifier': None,
  'address': None,
  'port': None,
  'onion_key': None,
  'service_key': None,
  'intro_authentication': [],
}  # type: Dict[str, Any]

# introduction-point fields that can only appear once

SINGLE_INTRODUCTION_POINT_FIELDS = [
  'introduction-point',
  'ip-address',
  'onion-port',
  'onion-key',
  'service-key',
]

BASIC_AUTH = 1
STEALTH_AUTH = 2
CHECKSUM_CONSTANT = b'.onion checksum'

SALT_LEN = 16
MAC_LEN = 32

S_KEY_LEN = 32
S_IV_LEN = 16


class DecryptionFailure(Exception):
  """
  Failure to decrypt the hidden service descriptor's introduction-points.
  """


class IntroductionPointV2(collections.namedtuple('IntroductionPointV2', INTRODUCTION_POINTS_ATTR.keys())):  # type: ignore
  """
  Introduction point for a v2 hidden service.

  :var str identifier: hash of this introduction point's identity key
  :var str address: address of this introduction point
  :var int port: port where this introduction point is listening
  :var str onion_key: public key for communicating with this introduction point
  :var str service_key: public key for communicating with this hidden service
  :var list intro_authentication: tuples of the form (auth_type, auth_data) for
    establishing a connection
  """


class IntroductionPointV3(collections.namedtuple('IntroductionPointV3', ['link_specifiers', 'onion_key_raw', 'auth_key_cert', 'enc_key_raw', 'enc_key_cert', 'legacy_key_raw', 'legacy_key_cert'])):
  """
  Introduction point for a v3 hidden service.

  .. versionadded:: 1.8.0

  :var list link_specifiers: :class:`~stem.client.datatype.LinkSpecifier` where this service is reachable
  :var unicode onion_key_raw: base64 ntor introduction point public key
  :var stem.descriptor.certificate.Ed25519Certificate auth_key_cert: cross-certifier of the signing key with the auth key
  :var unicode enc_key_raw: base64 introduction request encryption key
  :var stem.descriptor.certificate.Ed25519Certificate enc_key_cert: cross-certifier of the signing key by the encryption key
  :var str legacy_key_raw: base64 legacy introduction point RSA public key
  :var str legacy_key_cert: base64 cross-certifier of the signing key by the legacy key
  """

  @staticmethod
  def parse(content: bytes) -> 'stem.descriptor.hidden_service.IntroductionPointV3':
    """
    Parses an introduction point from its descriptor content.

    :param content: descriptor content to parse

    :returns: :class:`~stem.descriptor.hidden_service.IntroductionPointV3` for the descriptor content

    :raises: **ValueError** if descriptor content is malformed
    """

    entry = _descriptor_components(content, False)
    link_specifiers = IntroductionPointV3._parse_link_specifiers(stem.util.str_tools._to_bytes(_value('introduction-point', entry)))

    onion_key_line = _value('onion-key', entry)
    onion_key = onion_key_line[5:] if onion_key_line.startswith('ntor ') else None

    _, block_type, auth_key_cert = entry['auth-key'][0]
    auth_key_cert = Ed25519Certificate.from_base64(auth_key_cert)

    if block_type != 'ED25519 CERT':
      raise ValueError(
          f'Expected auth-key to have an ed25519 certificate, but was {block_type}'
      )

    enc_key_line = _value('enc-key', entry)
    enc_key = enc_key_line[5:] if enc_key_line.startswith('ntor ') else None

    _, block_type, enc_key_cert = entry['enc-key-cert'][0]
    enc_key_cert = Ed25519Certificate.from_base64(enc_key_cert)

    if block_type != 'ED25519 CERT':
      raise ValueError(
          f'Expected enc-key-cert to have an ed25519 certificate, but was {block_type}'
      )

    legacy_key = entry['legacy-key'][0][2] if 'legacy-key' in entry else None
    legacy_key_cert = entry['legacy-key-cert'][0][2] if 'legacy-key-cert' in entry else None

    return IntroductionPointV3(link_specifiers, onion_key, auth_key_cert, enc_key, enc_key_cert, legacy_key, legacy_key_cert)

  @staticmethod
  def create_for_address(address: str, port: int, expiration: Optional[datetime.datetime] = None, onion_key: Optional[str] = None, enc_key: Optional[str] = None, auth_key: Optional[str] = None, signing_key: Optional['cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey'] = None) -> 'stem.descriptor.hidden_service.IntroductionPointV3':  # type: ignore
    """
    Simplified constructor for a single address/port link specifier.

    :param address: IPv4 or IPv6 address where the service is reachable
    :param port: port where the service is reachable
    :param expiration: when certificates should expire
    :param onion_key: encoded, X25519PublicKey, or X25519PrivateKey onion key
    :param enc_key: encoded, X25519PublicKey, or X25519PrivateKey encryption key
    :param auth_key: encoded, Ed25519PublicKey, or Ed25519PrivateKey authentication key
    :param signing_key: service signing key

    :returns: :class:`~stem.descriptor.hidden_service.IntroductionPointV3` with these attributes

    :raises:
      * **ValueError** if the address, port, or keys are malformed
      * **ImportError** if cryptography module with ed25519 support is unavailable
    """

    if not stem.util.connection.is_valid_port(port):
      raise ValueError(f"'{port}' is an invalid port")

    link_specifiers = None  # type: Optional[List[stem.client.datatype.LinkSpecifier]]

    if stem.util.connection.is_valid_ipv4_address(address):
      link_specifiers = [stem.client.datatype.LinkByIPv4(address, port)]
    elif stem.util.connection.is_valid_ipv6_address(address):
      link_specifiers = [stem.client.datatype.LinkByIPv6(address, port)]
    else:
      raise ValueError(f"'{address}' is not a valid IPv4 or IPv6 address")

    return IntroductionPointV3.create_for_link_specifiers(link_specifiers, expiration = None, onion_key = None, enc_key = None, auth_key = None, signing_key = None)

  @staticmethod
  def create_for_link_specifiers(link_specifiers: Sequence['stem.client.datatype.LinkSpecifier'], expiration: Optional[datetime.datetime] = None, onion_key: Optional[str] = None, enc_key: Optional[str] = None, auth_key: Optional[str] = None, signing_key: Optional['cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey'] = None) -> 'stem.descriptor.hidden_service.IntroductionPointV3':  # type: ignore
    """
    Simplified constructor. For more sophisticated use cases you can use this
    as a template for how introduction points are properly created.

    :param link_specifiers: series of stem.client.datatype.LinkSpecifier where the service is reachable
    :param expiration: when certificates should expire
    :param onion_key: encoded, X25519PublicKey, or X25519PrivateKey onion key
    :param enc_key: encoded, X25519PublicKey, or X25519PrivateKey encryption key
    :param auth_key: encoded, Ed25519PublicKey, or Ed25519PrivateKey authentication key
    :param signing_key: service signing key

    :returns: :class:`~stem.descriptor.hidden_service.IntroductionPointV3` with these attributes

    :raises:
      * **ValueError** if the address, port, or keys are malformed
      * **ImportError** if cryptography module with ed25519 support is unavailable
    """

    try:
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    except ImportError:
      raise ImportError('Introduction point creation requires cryptography 2.6 or later')

    if expiration is None:
      expiration = datetime.datetime.utcnow() + datetime.timedelta(hours = stem.descriptor.certificate.DEFAULT_EXPIRATION_HOURS)

    onion_key = stem.util.str_tools._to_unicode(base64.b64encode(stem.util._pubkey_bytes(onion_key if onion_key else X25519PrivateKey.generate())))
    enc_key = stem.util.str_tools._to_unicode(base64.b64encode(stem.util._pubkey_bytes(enc_key if enc_key else X25519PrivateKey.generate())))
    auth_key = stem.util._pubkey_bytes(auth_key if auth_key else Ed25519PrivateKey.generate())
    signing_key = signing_key if signing_key else Ed25519PrivateKey.generate()

    extensions = [Ed25519Extension(ExtensionType.HAS_SIGNING_KEY, None, stem.util._pubkey_bytes(signing_key))]
    auth_key_cert = Ed25519CertificateV1(CertType.HS_V3_INTRO_AUTH, expiration, 1, auth_key, extensions, signing_key = signing_key)
    enc_key_cert = Ed25519CertificateV1(CertType.HS_V3_NTOR_ENC, expiration, 1, auth_key, extensions, signing_key = signing_key)

    return IntroductionPointV3(link_specifiers, onion_key, auth_key_cert, enc_key, enc_key_cert, None, None)

  def encode(self) -> str:
    """
    Descriptor representation of this introduction point.

    :returns: **str** for our descriptor representation
    """

    link_count = stem.client.datatype.Size.CHAR.pack(len(self.link_specifiers))
    link_specifiers = link_count + b''.join([link.pack() for link in self.link_specifiers])
    lines = [
        f'introduction-point {stem.util.str_tools._to_unicode(base64.b64encode(link_specifiers))}',
        f'onion-key ntor {self.onion_key_raw}',
        'auth-key\n' + self.auth_key_cert.to_base64(pem=True),
    ]
    if self.enc_key_raw:
      lines.append(f'enc-key ntor {self.enc_key_raw}')

    lines.append('enc-key-cert\n' + self.enc_key_cert.to_base64(pem = True))

    if self.legacy_key_raw:
      lines.append('legacy-key\n' + self.legacy_key_raw)

    if self.legacy_key_cert:
      lines.append('legacy-key-cert\n' + self.legacy_key_cert)

    return '\n'.join(lines)

  def onion_key(self) -> 'cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey':  # type: ignore
    """
    Provides our ntor introduction point public key.

    :returns: ntor :class:`~cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey`

    :raises:
      * **ImportError** if cryptography module with ed25519 support is unavailable
      * **EnvironmentError** if OpenSSL x25519 unsupported
    """

    return IntroductionPointV3._key_as(self.onion_key_raw, x25519 = True)

  def auth_key(self) -> 'cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey':  # type: ignore
    """
    Provides our authentication certificate's public key.

    :returns: :class:`~cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey`

    :raises:
      * **ImportError** if cryptography module with ed25519 support is unavailable
      * **EnvironmentError** if OpenSSL x25519 unsupported
    """

    return IntroductionPointV3._key_as(self.auth_key_cert.key, ed25519 = True)

  def enc_key(self) -> 'cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey':  # type: ignore
    """
    Provides our encryption key.

    :returns: encryption :class:`~cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey`

    :raises:
      * **ImportError** if cryptography module with ed25519 support is unavailable
      * **EnvironmentError** if OpenSSL x25519 unsupported
    """

    return IntroductionPointV3._key_as(self.enc_key_raw, x25519 = True)

  def legacy_key(self) -> 'cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey':  # type: ignore
    """
    Provides our legacy introduction point public key.

    :returns: legacy :class:`~cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey`

    :raises:
      * **ImportError** if cryptography module with ed25519 support is unavailable
      * **EnvironmentError** if OpenSSL x25519 unsupported
    """

    return IntroductionPointV3._key_as(self.legacy_key_raw, x25519 = True)

  @staticmethod
  def _key_as(value: bytes, x25519: bool = False, ed25519: bool = False) -> Union['cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey', 'cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey']:  # type: ignore
    if value is None or (not x25519 and not ed25519):
      return value

    try:
      from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
      raise ImportError('Key parsing requires cryptography 2.6 or later')

    if x25519:
      if X25519_AVAILABLE:
        return X25519PublicKey.from_public_bytes(base64.b64decode(value))

      else:
        # without this the cryptography raises...
        # cryptography.exceptions.UnsupportedAlgorithm: X25519 is not supported by this version of OpenSSL.

        raise EnvironmentError('OpenSSL x25519 unsupported')

    return Ed25519PublicKey.from_public_bytes(value)

  @staticmethod
  def _parse_link_specifiers(content: bytes) -> List['stem.client.datatype.LinkSpecifier']:
    try:
      content = base64.b64decode(content)
    except Exception as exc:
      raise ValueError(
          f'Unable to base64 decode introduction point ({exc}): {stem.util.str_tools._to_unicode(content)}'
      )

    link_specifiers = []
    count, content = stem.client.datatype.Size.CHAR.pop(content)

    for _ in range(count):
      link_specifier, content = stem.client.datatype.LinkSpecifier.pop(content)
      link_specifiers.append(link_specifier)

    if content:
      raise ValueError(
          f'Introduction point had excessive data ({stem.util.str_tools._to_unicode(content)})'
      )

    return link_specifiers

  def __hash__(self) -> int:
    if not hasattr(self, '_hash'):
      self._hash = hash(self.encode())

    return self._hash

  def __eq__(self, other: Any) -> bool:
    return hash(self) == hash(other) if isinstance(other, IntroductionPointV3) else False

  def __ne__(self, other: Any) -> bool:
    return not self == other


class AuthorizedClient(object):
  """
  Client authorized to use a v3 hidden service.

  .. versionadded:: 1.8.0

  :var str id: base64 encoded client id
  :var str iv: base64 encoded randomized initialization vector
  :var str cookie: base64 encoded authentication cookie
  """

  def __init__(self, id: Optional[str] = None, iv: Optional[str] = None, cookie: Optional[str] = None) -> None:
    self.id = stem.util.str_tools._to_unicode(id if id else base64.b64encode(os.urandom(8)).rstrip(b'='))
    self.iv = stem.util.str_tools._to_unicode(iv if iv else base64.b64encode(os.urandom(16)).rstrip(b'='))
    self.cookie = stem.util.str_tools._to_unicode(cookie if cookie else base64.b64encode(os.urandom(16)).rstrip(b'='))

  def __hash__(self) -> int:
    return stem.util._hash_attr(self, 'id', 'iv', 'cookie', cache = True)

  def __eq__(self, other: Any) -> bool:
    return hash(self) == hash(other) if isinstance(other, AuthorizedClient) else False

  def __ne__(self, other: Any) -> bool:
    return not self == other


def _parse_file(descriptor_file: BinaryIO, desc_type: Optional[Type['stem.descriptor.hidden_service.HiddenServiceDescriptor']] = None, validate: bool = False, **kwargs: Any) -> Iterator['stem.descriptor.hidden_service.HiddenServiceDescriptor']:
  """
  Iterates over the hidden service descriptors in a file.

  :param descriptor_file: file with descriptor content
  :param desc_type: HiddenServiceDescriptor subclass
  :param validate: checks the validity of the descriptor's content if
    **True**, skips these checks otherwise
  :param kwargs: additional arguments for the descriptor constructor

  :returns: iterator for :class:`~stem.descriptor.hidden_service.HiddenServiceDescriptor`
    instances in the file

  :raises:
    * **ValueError** if the contents is malformed and validate is **True**
    * **OSError** if the file can't be read
  """

  if desc_type is None:
    desc_type = HiddenServiceDescriptorV2

  # Hidden service v3 ends with a signature line, whereas v2 has a pgp style
  # block following it.

  while True:
    descriptor_content = _read_until_keywords('signature', descriptor_file, True)

    if desc_type == HiddenServiceDescriptorV2:
      block_end_prefix = PGP_BLOCK_END.split(' ', 1)[0]
      descriptor_content += _read_until_keywords(block_end_prefix, descriptor_file, True)

    if descriptor_content:
      if descriptor_content[0].startswith(b'@type'):
        descriptor_content = descriptor_content[1:]

      yield desc_type(bytes.join(b'', descriptor_content), validate, **kwargs)  # type: ignore
    else:
      break  # done parsing file


def _decrypt_layer(encrypted_block: str, constant: bytes, revision_counter: int, subcredential: bytes, blinded_key: bytes) -> str:
  if encrypted_block.startswith('-----BEGIN MESSAGE-----\n') and encrypted_block.endswith('\n-----END MESSAGE-----'):
    encrypted_block = encrypted_block[24:-22]

  try:
    encrypted = base64.b64decode(encrypted_block)
  except:
    raise ValueError('Unable to decode encrypted block as base64')

  if len(encrypted) < SALT_LEN + MAC_LEN:
    raise ValueError('Encrypted block malformed (only %i bytes)' % len(encrypted))

  salt = encrypted[:SALT_LEN]
  ciphertext = encrypted[SALT_LEN:-MAC_LEN]
  expected_mac = encrypted[-MAC_LEN:]

  cipher, mac_for = _layer_cipher(constant, revision_counter, subcredential, blinded_key, salt)

  if expected_mac != mac_for(ciphertext):
    raise ValueError(
        f'Malformed mac (expected {stem.util.str_tools._to_unicode(expected_mac)}, but was {stem.util.str_tools._to_unicode(mac_for(ciphertext))})'
    )

  decryptor = cipher.decryptor()
  plaintext = decryptor.update(ciphertext) + decryptor.finalize()

  return stem.util.str_tools._to_unicode(plaintext)


def _encrypt_layer(plaintext: bytes, constant: bytes, revision_counter: int, subcredential: bytes, blinded_key: bytes) -> bytes:
  salt = os.urandom(16)
  cipher, mac_for = _layer_cipher(constant, revision_counter, subcredential, blinded_key, salt)

  encryptor = cipher.encryptor()
  ciphertext = encryptor.update(plaintext) + encryptor.finalize()
  encoded = base64.b64encode(salt + ciphertext + mac_for(ciphertext))

  return b'-----BEGIN MESSAGE-----\n%s\n-----END MESSAGE-----' % b'\n'.join(stem.util.str_tools._split_by_length(encoded, 64))


def _layer_cipher(constant: bytes, revision_counter: int, subcredential: bytes, blinded_key: bytes, salt: bytes) -> Tuple['cryptography.hazmat.primitives.ciphers.Cipher', Callable[[bytes], bytes]]:  # type: ignore
  try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
  except ImportError:
    raise ImportError('Layer encryption/decryption requires the cryptography module')

  kdf = hashlib.shake_256(blinded_key + subcredential + struct.pack('>Q', revision_counter) + salt + constant)
  keys = kdf.digest(S_KEY_LEN + S_IV_LEN + MAC_LEN)

  secret_key = keys[:S_KEY_LEN]
  secret_iv = keys[S_KEY_LEN:S_KEY_LEN + S_IV_LEN]
  mac_key = keys[S_KEY_LEN + S_IV_LEN:]

  cipher = Cipher(algorithms.AES(secret_key), modes.CTR(secret_iv), default_backend())
  mac_prefix = struct.pack('>Q', len(mac_key)) + mac_key + struct.pack('>Q', len(salt)) + salt

  return cipher, lambda ciphertext: hashlib.sha3_256(mac_prefix + ciphertext).digest()


def _parse_protocol_versions_line(descriptor: 'stem.descriptor.Descriptor', entries: ENTRY_TYPE) -> None:
  value = _value('protocol-versions', entries)

  try:
    versions = [int(entry) for entry in value.split(',')]
  except ValueError:
    raise ValueError(
        f'protocol-versions line has non-numeric versoins: protocol-versions {value}'
    )

  for v in versions:
    if v <= 0:
      raise ValueError(f'protocol-versions must be positive integers: {value}')

  descriptor.protocol_versions = versions


def _parse_introduction_points_line(descriptor: 'stem.descriptor.Descriptor', entries: ENTRY_TYPE) -> None:
  _, block_type, block_contents = entries['introduction-points'][0]

  if not block_contents or block_type != 'MESSAGE':
    raise ValueError(
        f"'introduction-points' should be followed by a MESSAGE block, but was a {block_type}"
    )

  descriptor.introduction_points_encoded = block_contents

  try:
    descriptor.introduction_points_content = _bytes_for_block(block_contents)
  except TypeError:
    raise ValueError("'introduction-points' isn't base64 encoded content:\n%s" % block_contents)


def _parse_v3_outer_clients(descriptor: 'stem.descriptor.Descriptor', entries: ENTRY_TYPE) -> None:
  # "auth-client" client-id iv encrypted-cookie

  clients = {}

  for value in _values('auth-client', entries):
    value_comp = value.split()

    if len(value_comp) < 3:
      raise ValueError(
          f'auth-client should have a client-id, iv, and cookie: auth-client {value}'
      )

    clients[value_comp[0]] = AuthorizedClient(value_comp[0], value_comp[1], value_comp[2])

  descriptor.clients = clients


def _parse_v3_inner_formats(descriptor: 'stem.descriptor.Descriptor', entries: ENTRY_TYPE) -> None:
  value, formats = _value('create2-formats', entries), []

  for entry in value.split(' '):
    if not entry.isdigit():
      raise ValueError(
          f"create2-formats should only contain integers, but was '{value}'")

    formats.append(int(entry))

  descriptor.formats = formats


def _parse_v3_introduction_points(descriptor: 'stem.descriptor.Descriptor', entries: ENTRY_TYPE) -> None:
  if hasattr(descriptor, '_unparsed_introduction_points'):
    introduction_points = []
    remaining = descriptor._unparsed_introduction_points

    while remaining:
      div = remaining.find(b'\nintroduction-point ', 10)
      content, remaining = (remaining[:div], remaining[div + 1:]) if div != -1 else (remaining, '')

      introduction_points.append(IntroductionPointV3.parse(content))

    descriptor.introduction_points = introduction_points
    del descriptor._unparsed_introduction_points


_parse_v2_version_line = _parse_int_line('version', 'version', allow_negative = False)
_parse_rendezvous_service_descriptor_line = _parse_simple_line('rendezvous-service-descriptor', 'descriptor_id')
_parse_permanent_key_line = _parse_key_block('permanent-key', 'permanent_key', 'RSA PUBLIC KEY')
_parse_secret_id_part_line = _parse_simple_line('secret-id-part', 'secret_id_part')
_parse_publication_time_line = _parse_timestamp_line('publication-time', 'published')
_parse_v2_signature_line = _parse_key_block('signature', 'signature', 'SIGNATURE')

_parse_v3_version_line = _parse_int_line('hs-descriptor', 'version', allow_negative = False)
_parse_lifetime_line = _parse_int_line('descriptor-lifetime', 'lifetime', allow_negative = False)
_parse_signing_cert = Ed25519Certificate._from_descriptor('descriptor-signing-key-cert', 'signing_cert')
_parse_revision_counter_line = _parse_int_line('revision-counter', 'revision_counter', allow_negative = False)
_parse_superencrypted_line = _parse_key_block('superencrypted', 'superencrypted', 'MESSAGE')
_parse_v3_signature_line = _parse_simple_line('signature', 'signature')

_parse_v3_outer_auth_type = _parse_simple_line('desc-auth-type', 'auth_type')
_parse_v3_outer_ephemeral_key = _parse_simple_line('desc-auth-ephemeral-key', 'ephemeral_key')
_parse_v3_outer_encrypted = _parse_key_block('encrypted', 'encrypted', 'MESSAGE')

_parse_v3_inner_intro_auth = _parse_simple_line('intro-auth-required', 'intro_auth', func = lambda v: v.split(' '))
_parse_v3_inner_single_service = _parse_if_present('single-onion-service', 'is_single_service')


class HiddenServiceDescriptor(Descriptor):
  """
  Hidden service descriptor.

  .. versionadded:: 1.8.0
  """


class HiddenServiceDescriptorV2(HiddenServiceDescriptor):
  """
  Version 2 hidden service descriptor.

  :var str descriptor_id: **\\*** identifier for this descriptor, this is a base32 hash of several fields
  :var int version: **\\*** hidden service descriptor version
  :var str permanent_key: **\\*** long term key of the hidden service
  :var str secret_id_part: **\\*** hash of the time period, cookie, and replica
    values so our descriptor_id can be validated
  :var datetime published: **\\*** time in UTC when this descriptor was made
  :var list protocol_versions: **\\*** list of **int** versions that are supported when establishing a connection
  :var str introduction_points_encoded: raw introduction points blob
  :var bytes introduction_points_content: decoded introduction-points content
    without authentication data, if using cookie authentication this is
    encrypted
  :var str signature: signature of the descriptor content

  **\\*** attribute is either required when we're parsed with validation or has
  a default value, others are left as **None** if undefined

  .. versionchanged:: 1.6.0
     Moved from the deprecated `pycrypto
     <https://www.dlitz.net/software/pycrypto/>`_ module to `cryptography
     <https://pypi.org/project/cryptography/>`_ for validating signatures.

  .. versionchanged:: 1.6.0
     Added the **skip_crypto_validation** constructor argument.
  """

  TYPE_ANNOTATION_NAME = 'hidden-service-descriptor'

  ATTRIBUTES = {
    'descriptor_id': (None, _parse_rendezvous_service_descriptor_line),
    'version': (None, _parse_v2_version_line),
    'permanent_key': (None, _parse_permanent_key_line),
    'secret_id_part': (None, _parse_secret_id_part_line),
    'published': (None, _parse_publication_time_line),
    'protocol_versions': ([], _parse_protocol_versions_line),
    'introduction_points_encoded': (None, _parse_introduction_points_line),
    'introduction_points_content': (None, _parse_introduction_points_line),
    'signature': (None, _parse_v2_signature_line),
  }  # type: Dict[str, Tuple[Any, Callable[['stem.descriptor.Descriptor', ENTRY_TYPE], None]]]

  PARSER_FOR_LINE = {
    'rendezvous-service-descriptor': _parse_rendezvous_service_descriptor_line,
    'version': _parse_v2_version_line,
    'permanent-key': _parse_permanent_key_line,
    'secret-id-part': _parse_secret_id_part_line,
    'publication-time': _parse_publication_time_line,
    'protocol-versions': _parse_protocol_versions_line,
    'introduction-points': _parse_introduction_points_line,
    'signature': _parse_v2_signature_line,
  }

  @classmethod
  def content(cls: Type['stem.descriptor.hidden_service.HiddenServiceDescriptorV2'], attr: Mapping[str, str] = None, exclude: Sequence[str] = ()) -> bytes:
    return _descriptor_content(attr, exclude, (
      ('rendezvous-service-descriptor', 'y3olqqblqw2gbh6phimfuiroechjjafa'),
      ('version', '2'),
      ('permanent-key', _random_crypto_blob('RSA PUBLIC KEY')),
      ('secret-id-part', 'e24kgecavwsznj7gpbktqsiwgvngsf4e'),
      ('publication-time', _random_date()),
      ('protocol-versions', '2,3'),
      ('introduction-points', '\n-----BEGIN MESSAGE-----\n-----END MESSAGE-----'),
    ), (
      ('signature', _random_crypto_blob('SIGNATURE')),
    ))

  @classmethod
  def create(cls: Type['stem.descriptor.hidden_service.HiddenServiceDescriptorV2'], attr: Mapping[str, str] = None, exclude: Sequence[str] = (), validate: bool = True) -> 'stem.descriptor.hidden_service.HiddenServiceDescriptorV2':
    return cls(cls.content(attr, exclude), validate = validate, skip_crypto_validation = True)

  def __init__(self, raw_contents: bytes, validate: bool = False, skip_crypto_validation: bool = False) -> None:
    super(HiddenServiceDescriptorV2, self).__init__(raw_contents, lazy_load = not validate)
    entries = _descriptor_components(raw_contents, validate, non_ascii_fields = ('introduction-points'))

    if validate:
      for keyword in REQUIRED_V2_FIELDS:
        if keyword not in entries:
          raise ValueError(f"Hidden service descriptor must have a '{keyword}' entry")
        elif len(entries[keyword]) > 1:
          raise ValueError(
              f"The '{keyword}' entry can only appear once in a hidden service descriptor"
          )

      if list(entries.keys())[0] != 'rendezvous-service-descriptor':
        raise ValueError("Hidden service descriptor must start with a 'rendezvous-service-descriptor' entry")
      elif list(entries.keys())[-1] != 'signature':
        raise ValueError("Hidden service descriptor must end with a 'signature' entry")

      self._parse(entries, validate)

      if not skip_crypto_validation:
        try:
          signed_digest = self._digest_for_signature(self.permanent_key, self.signature)
          digest_content = self._content_range('rendezvous-service-descriptor ', '\nsignature\n')
          content_digest = hashlib.sha1(digest_content).hexdigest().upper()

          if signed_digest != content_digest:
            raise ValueError(
                f'Decrypted digest does not match local digest (calculated: {signed_digest}, local: {content_digest})'
            )
        except ImportError:
          pass  # cryptography module unavailable
    else:
      self._entries = entries

  @functools.lru_cache()
  def introduction_points(self, authentication_cookie: Optional[bytes] = None) -> Sequence['stem.descriptor.hidden_service.IntroductionPointV2']:
    """
    Provided this service's introduction points.

    :param authentication_cookie: base64 encoded authentication cookie

    :returns: **list** of :class:`~stem.descriptor.hidden_service.IntroductionPointV2`

    :raises:
      * **ValueError** if the our introduction-points is malformed
      * **DecryptionFailure** if unable to decrypt this field
    """

    content = self.introduction_points_content

    if not content:
      return []
    elif authentication_cookie:
      try:
        authentication_cookie = stem.util.str_tools._decode_b64(authentication_cookie)
      except TypeError as exc:
        raise DecryptionFailure(
            f'authentication_cookie must be a base64 encoded string ({exc})')

      authentication_type = int(binascii.hexlify(content[:1]), 16)

      if authentication_type == BASIC_AUTH:
        content = HiddenServiceDescriptorV2._decrypt_basic_auth(content, authentication_cookie)
      elif authentication_type == STEALTH_AUTH:
        content = HiddenServiceDescriptorV2._decrypt_stealth_auth(content, authentication_cookie)
      else:
        raise DecryptionFailure(
            f"Unrecognized authentication type '{authentication_type}', currently we only support basic auth ({BASIC_AUTH}) and stealth auth ({STEALTH_AUTH})"
        )

      if not content.startswith(b'introduction-point '):
        raise DecryptionFailure('Unable to decrypt the introduction-points, maybe this is the wrong key?')
    elif not content.startswith(b'introduction-point '):
      raise DecryptionFailure('introduction-points content is encrypted, you need to provide its authentication_cookie')

    return HiddenServiceDescriptorV2._parse_introduction_points(content)

  @staticmethod
  def _decrypt_basic_auth(content: bytes, authentication_cookie: bytes) -> bytes:
    try:
      from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
      from cryptography.hazmat.backends import default_backend
    except ImportError:
      raise DecryptionFailure('Decrypting introduction-points requires the cryptography module')

    try:
      client_blocks = int(binascii.hexlify(content[1:2]), 16)
    except ValueError:
      raise DecryptionFailure(
          f"When using basic auth the content should start with a number of blocks but wasn't a hex digit: {binascii.hexlify(content[1:2]).decode('utf-8')}"
      )

    # parse the client id and encrypted session keys

    client_entries_length = client_blocks * 16 * 20
    client_entries = content[2:2 + client_entries_length]
    client_keys = [(client_entries[i:i + 4], client_entries[i + 4:i + 20]) for i in range(0, client_entries_length, 4 + 16)]

    iv = content[2 + client_entries_length:2 + client_entries_length + 16]
    encrypted = content[2 + client_entries_length + 16:]

    client_id = hashlib.sha1(authentication_cookie + iv).digest()[:4]

    for entry_id, encrypted_session_key in client_keys:
      if entry_id != client_id:
        continue  # not the session key for this client

      # try decrypting the session key

      cipher = Cipher(algorithms.AES(authentication_cookie), modes.CTR(b'\x00' * len(iv)), default_backend())
      decryptor = cipher.decryptor()
      session_key = decryptor.update(encrypted_session_key) + decryptor.finalize()

      # attempt to decrypt the intro points with the session key

      cipher = Cipher(algorithms.AES(session_key), modes.CTR(iv), default_backend())
      decryptor = cipher.decryptor()
      decrypted = decryptor.update(encrypted) + decryptor.finalize()

      # check if the decryption looks correct

      if decrypted.startswith(b'introduction-point '):
        return decrypted

    return content  # nope, unable to decrypt the content

  @staticmethod
  def _decrypt_stealth_auth(content: bytes, authentication_cookie: bytes) -> bytes:
    try:
      from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
      from cryptography.hazmat.backends import default_backend
    except ImportError:
      raise DecryptionFailure('Decrypting introduction-points requires the cryptography module')

    # byte 1 = authentication type, 2-17 = input vector, 18 on = encrypted content
    iv, encrypted = content[1:17], content[17:]
    cipher = Cipher(algorithms.AES(authentication_cookie), modes.CTR(iv), default_backend())
    decryptor = cipher.decryptor()

    return decryptor.update(encrypted) + decryptor.finalize()

  @staticmethod
  def _parse_introduction_points(content: bytes) -> Sequence['stem.descriptor.hidden_service.IntroductionPointV2']:
    """
    Provides the parsed list of IntroductionPointV2 for the unencrypted content.
    """

    introduction_points = []
    content_io = io.BytesIO(content)

    while True:
      content = b''.join(_read_until_keywords('introduction-point', content_io, ignore_first = True))

      if not content:
        break  # reached the end

      attr = dict(INTRODUCTION_POINTS_ATTR)
      entries = _descriptor_components(content, False)

      for keyword, values in list(entries.items()):
        value, block_type, block_contents = values[0]

        if keyword in SINGLE_INTRODUCTION_POINT_FIELDS and len(values) > 1:
          raise ValueError("'%s' can only appear once in an introduction-point block, but appeared %i times" % (keyword, len(values)))

        if keyword == 'introduction-point':
          attr['identifier'] = value
        elif keyword == 'ip-address':
          if not stem.util.connection.is_valid_ipv4_address(value):
            raise ValueError(f"'{value}' is an invalid IPv4 address")

          attr['address'] = value
        elif keyword == 'onion-port':
          if not stem.util.connection.is_valid_port(value):
            raise ValueError(f"'{value}' is an invalid port")

          attr['port'] = int(value)
        elif keyword == 'onion-key':
          attr['onion_key'] = block_contents
        elif keyword == 'service-key':
          attr['service_key'] = block_contents
        elif keyword == 'intro-authentication':
          auth_entries = []

          for auth_value, _, _ in values:
            if ' ' not in auth_value:
              raise ValueError(
                  f"We expected 'intro-authentication [auth_type] [auth_data]', but had '{auth_value}'"
              )

            auth_type, auth_data = auth_value.split(' ')[:2]
            auth_entries.append((auth_type, auth_data))

      introduction_points.append(IntroductionPointV2(**attr))  # type: ignore

    return introduction_points


class HiddenServiceDescriptorV3(HiddenServiceDescriptor):
  """
  Version 3 hidden service descriptor.

  :var int version: **\\*** hidden service descriptor version
  :var int lifetime: **\\*** minutes after publication this descriptor is valid
  :var stem.descriptor.certificate.Ed25519Certificate signing_cert: **\\*** cross-certifier for the short-term descriptor signing key
  :var int revision_counter: **\\*** descriptor revision number
  :var str superencrypted: **\\*** encrypted HS-DESC-ENC payload
  :var str signature: **\\*** signature of this descriptor

  **\\*** attribute is either required when we're parsed with validation or has
  a default value, others are left as **None** if undefined

  .. versionadded:: 1.8.0
  """

  TYPE_ANNOTATION_NAME = 'hidden-service-descriptor-3'

  ATTRIBUTES = {
    'version': (None, _parse_v3_version_line),
    'lifetime': (None, _parse_lifetime_line),
    'signing_cert': (None, _parse_signing_cert),
    'revision_counter': (None, _parse_revision_counter_line),
    'superencrypted': (None, _parse_superencrypted_line),
    'signature': (None, _parse_v3_signature_line),
  }

  PARSER_FOR_LINE = {
    'hs-descriptor': _parse_v3_version_line,
    'descriptor-lifetime': _parse_lifetime_line,
    'descriptor-signing-key-cert': _parse_signing_cert,
    'revision-counter': _parse_revision_counter_line,
    'superencrypted': _parse_superencrypted_line,
    'signature': _parse_v3_signature_line,
  }

  @classmethod
  def content(cls: Type['stem.descriptor.hidden_service.HiddenServiceDescriptorV3'], attr: Optional[Mapping[str, str]] = None, exclude: Sequence[str] = (), sign: bool = False, inner_layer: Optional['stem.descriptor.hidden_service.InnerLayer'] = None, outer_layer: Optional['stem.descriptor.hidden_service.OuterLayer'] = None, identity_key: Optional['cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey'] = None, signing_key: Optional['cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey'] = None, signing_cert: Optional['stem.descriptor.certificate.Ed25519CertificateV1'] = None, revision_counter: int = None, blinding_nonce: bytes = None) -> bytes:  # type: ignore
    """
    Hidden service v3 descriptors consist of three parts:

      * InnerLayer, which most notably contain introduction points where the
        service can be reached.

      * OuterLayer, which encrypts the InnerLayer among other paremters.

      * HiddenServiceDescriptorV3, which contains the OuterLayer and plaintext
        parameters.

    Construction through this method can supply any or none of these, with
    omitted parameters populated with randomized defaults.

    Ed25519 key blinding adds an additional ~20 ms, and as such is disabled by
    default. To blind with a random nonce simply call...

    ::

      HiddenServiceDescriptorV3.create(blinding_nonce = os.urandom(32))

    :param attr: keyword/value mappings to be included in plaintext descriptor
    :param exclude: mandatory keywords to exclude from the descriptor, this
      results in an invalid descriptor
    :param sign: includes cryptographic signatures and digests if True
    :param inner_layer: inner encrypted layer
    :param outer_layer: outer encrypted layer
    :param identity_key: service identity key
    :param signing_key: service signing key
    :param signing_cert: certificate signing this descriptor
    :param revision_counter: descriptor revision number
    :param blinding_nonce: 32 byte blinding factor to derive the blinding key

    :returns: **str** with the content of a descriptor

    :raises:
      * **ValueError** if parameters are malformed
      * **ImportError** if cryptography module with ed25519 support is unavailable
    """

    try:
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
      raise ImportError('Hidden service descriptor creation requires cryptography 2.6 or later')

    if blinding_nonce and len(blinding_nonce) != 32:
      raise ValueError('Blinding nonce must be 32 bytes, but was %i' % len(blinding_nonce))

    inner_layer = inner_layer if inner_layer else InnerLayer.create(exclude = exclude)
    identity_key = identity_key if identity_key else Ed25519PrivateKey.generate()
    signing_key = signing_key if signing_key else Ed25519PrivateKey.generate()
    revision_counter = revision_counter if revision_counter else int(time.time())

    blinded_key = _blinded_pubkey(identity_key, blinding_nonce) if blinding_nonce else b'a' * 32
    subcredential = HiddenServiceDescriptorV3._subcredential(identity_key, blinded_key)

    if attr and 'signature' in attr:
      custom_sig = attr['signature']
      attr = dict(filter(lambda entry: entry[0] != 'signature', attr.items()))
    else:
      custom_sig = None

    if not outer_layer:
      outer_layer = OuterLayer.create(
        exclude = exclude,
        inner_layer = inner_layer,
        revision_counter = revision_counter,
        subcredential = subcredential,
        blinded_key = blinded_key,
      )

    if not signing_cert:
      extensions = [Ed25519Extension(ExtensionType.HAS_SIGNING_KEY, None, blinded_key)]

      signing_cert = Ed25519CertificateV1(cert_type = CertType.HS_V3_DESC_SIGNING, key = signing_key, extensions = extensions)
      signing_cert.signature = _blinded_sign(signing_cert.pack(), identity_key, blinded_key, blinding_nonce) if blinding_nonce else b'b' * 64

    desc_content = _descriptor_content(attr, exclude, (
      ('hs-descriptor', '3'),
      ('descriptor-lifetime', '180'),
      ('descriptor-signing-key-cert', '\n' + signing_cert.to_base64(pem = True)),
      ('revision-counter', str(revision_counter)),
      ('superencrypted', stem.util.str_tools._to_unicode(b'\n' + outer_layer._encrypt(revision_counter, subcredential, blinded_key))),
    ), ()) + b'\n'

    if custom_sig:
      desc_content += b'signature %s' % stem.util.str_tools._to_bytes(custom_sig)
    elif 'signature' not in exclude:
      sig_content = stem.descriptor.certificate.SIG_PREFIX_HS_V3 + desc_content
      desc_content += b'signature %s' % base64.b64encode(signing_key.sign(sig_content)).rstrip(b'=')

    return desc_content

  @classmethod
  def create(cls: Type['stem.descriptor.hidden_service.HiddenServiceDescriptorV3'], attr: Optional[Mapping[str, str]] = None, exclude: Sequence[str] = (), validate: bool = True, sign: bool = False, inner_layer: Optional['stem.descriptor.hidden_service.InnerLayer'] = None, outer_layer: Optional['stem.descriptor.hidden_service.OuterLayer'] = None, identity_key: Optional['cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey'] = None, signing_key: Optional['cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey'] = None, signing_cert: Optional['stem.descriptor.certificate.Ed25519CertificateV1'] = None, revision_counter: int = None, blinding_nonce: bytes = None) -> 'stem.descriptor.hidden_service.HiddenServiceDescriptorV3':  # type: ignore
    return cls(cls.content(attr, exclude, sign, inner_layer, outer_layer, identity_key, signing_key, signing_cert, revision_counter, blinding_nonce), validate = validate)

  def __init__(self, raw_contents: bytes, validate: bool = False) -> None:
    super(HiddenServiceDescriptorV3, self).__init__(raw_contents, lazy_load = not validate)

    self._inner_layer = None  # type: Optional[stem.descriptor.hidden_service.InnerLayer]
    entries = _descriptor_components(raw_contents, validate)

    if validate:
      for keyword in REQUIRED_V3_FIELDS:
        if keyword not in entries:
          raise ValueError(f"Hidden service descriptor must have a '{keyword}' entry")
        elif len(entries[keyword]) > 1:
          raise ValueError(
              f"The '{keyword}' entry can only appear once in a hidden service descriptor"
          )

      if list(entries.keys())[0] != 'hs-descriptor':
        raise ValueError("Hidden service descriptor must start with a 'hs-descriptor' entry")
      elif list(entries.keys())[-1] != 'signature':
        raise ValueError("Hidden service descriptor must end with a 'signature' entry")

      self._parse(entries, validate)

      if self.signing_cert:
        try:
          self.signing_cert.validate(self)
        except ImportError:
          pass  # cryptography module unavailable
    else:
      self._entries = entries

  def decrypt(self, onion_address: str) -> 'stem.descriptor.hidden_service.InnerLayer':
    """
    Decrypt this descriptor. Hidden serice descriptors contain two encryption
    layers (:class:`~stem.descriptor.hidden_service.OuterLayer` and
    :class:`~stem.descriptor.hidden_service.InnerLayer`).

    :param onion_address: hidden service address this descriptor is from

    :returns: :class:`~stem.descriptor.hidden_service.InnerLayer` with our
      decrypted content

    :raises: **ValueError** if unable to decrypt or validation fails
    """

    if self._inner_layer is None:
      try:
        blinded_key = self.signing_cert.signing_key() if self.signing_cert else None

        if not blinded_key:
          raise ValueError('No signing key is present')

        identity_public_key = HiddenServiceDescriptorV3.identity_key_from_address(onion_address)
        subcredential = HiddenServiceDescriptorV3._subcredential(identity_public_key, blinded_key)

        outer_layer = OuterLayer._decrypt(self.superencrypted, self.revision_counter, subcredential, blinded_key)
        self._inner_layer = InnerLayer._decrypt(outer_layer, self.revision_counter, subcredential, blinded_key)
      except ImportError:
        raise ImportError('Hidden service descriptor decryption requires cryptography version 2.6')

    return self._inner_layer

  @staticmethod
  def address_from_identity_key(key: Union[bytes, 'cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey', 'cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey'], suffix: bool = True) -> str:  # type: ignore
    """
    Converts a hidden service identity key into its address. This accepts all
    key formats (private, public, or public bytes).

    :param key: hidden service identity key
    :param suffix: includes the '.onion' suffix if true, excluded otherwise

    :returns: **str** hidden service address

    :raises: **ImportError** if key is a cryptographic type and ed25519 support
      is unavailable
    """

    key = stem.util._pubkey_bytes(key)  # normalize key into bytes

    version = stem.client.datatype.Size.CHAR.pack(3)
    checksum = hashlib.sha3_256(CHECKSUM_CONSTANT + key + version).digest()[:2]
    onion_address = base64.b32encode(key + checksum + version)

    return stem.util.str_tools._to_unicode(onion_address + b'.onion' if suffix else onion_address).lower()

  @staticmethod
  def identity_key_from_address(onion_address: str) -> bytes:
    """
    Converts a hidden service address into its public identity key.

    :param onion_address: hidden service address

    :returns: **bytes** for the hidden service's public identity key

    :raises: **ValueError** if address malformed or checksum is invalid
    """

    onion_address = onion_address.removesuffix('.onion')
    if not stem.util.tor_tools.HS_V3_ADDRESS_PATTERN.match(onion_address):
      raise ValueError(
          f"'{onion_address}.onion' isn't a valid hidden service v3 address")

    # onion_address = base32(PUBKEY | CHECKSUM | VERSION) + '.onion'
    # CHECKSUM = H('.onion checksum' | PUBKEY | VERSION)[:2]

    decoded_address = base64.b32decode(onion_address.upper())

    pubkey = decoded_address[:32]
    expected_checksum = decoded_address[32:34]
    version = decoded_address[34:35]

    checksum = hashlib.sha3_256(CHECKSUM_CONSTANT + pubkey + version).digest()[:2]

    if expected_checksum != checksum:
      checksum_str = stem.util.str_tools._to_unicode(binascii.hexlify(checksum))
      expected_checksum_str = stem.util.str_tools._to_unicode(binascii.hexlify(expected_checksum))

      raise ValueError(
          f'Bad checksum (expected {expected_checksum_str} but was {checksum_str})'
      )

    return pubkey

  @staticmethod
  def _subcredential(identity_key: 'cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey', blinded_key: bytes) -> bytes:  # type: ignore
    # credential = H('credential' | public-identity-key)
    # subcredential = H('subcredential' | credential | blinded-public-key)

    credential = hashlib.sha3_256(b'credential%s' % stem.util._pubkey_bytes(identity_key)).digest()
    return hashlib.sha3_256(b'subcredential%s%s' % (credential, blinded_key)).digest()


class OuterLayer(Descriptor):
  """
  Initial encryped layer of a hidden service v3 descriptor (`spec
  <https://gitweb.torproject.org/torspec.git/tree/rend-spec-v3.txt>`_ section
  2.5.1.2).

  .. versionadded:: 1.8.0

  :var str auth_type: **\\*** encryption scheme used for descriptor authorization
  :var str ephemeral_key: **\\*** base64 encoded x25519 public key
  :var dict clients: **\\*** mapping of authorized client ids to their
    :class:`~stem.descriptor.hidden_service.AuthorizedClient`
  :var str encrypted: **\\*** encrypted descriptor inner layer

  **\\*** attribute is either required when we're parsed with validation or has
  a default value, others are left as **None** if undefined
  """

  ATTRIBUTES = {
    'auth_type': (None, _parse_v3_outer_auth_type),
    'ephemeral_key': (None, _parse_v3_outer_ephemeral_key),
    'clients': ({}, _parse_v3_outer_clients),
    'encrypted': (None, _parse_v3_outer_encrypted),
  }  # type: Dict[str, Tuple[Any, Callable[['stem.descriptor.Descriptor', ENTRY_TYPE], None]]]

  PARSER_FOR_LINE = {
    'desc-auth-type': _parse_v3_outer_auth_type,
    'desc-auth-ephemeral-key': _parse_v3_outer_ephemeral_key,
    'auth-client': _parse_v3_outer_clients,
    'encrypted': _parse_v3_outer_encrypted,
  }

  @staticmethod
  def _decrypt(encrypted: str, revision_counter: int, subcredential: bytes, blinded_key: bytes) -> 'stem.descriptor.hidden_service.OuterLayer':
    plaintext = _decrypt_layer(encrypted, b'hsdir-superencrypted-data', revision_counter, subcredential, blinded_key)
    return OuterLayer(stem.util.str_tools._to_bytes(plaintext))

  def _encrypt(self, revision_counter: int, subcredential: bytes, blinded_key: bytes) -> bytes:
    # Spec mandated padding: "Before encryption the plaintext is padded with
    # NUL bytes to the nearest multiple of 10k bytes."

    content = self.get_bytes() + b'\x00' * (len(self.get_bytes()) % 10000)

    # encrypt back into a hidden service descriptor's 'superencrypted' field

    return _encrypt_layer(content, b'hsdir-superencrypted-data', revision_counter, subcredential, blinded_key)

  @classmethod
  def content(cls: Type['stem.descriptor.hidden_service.OuterLayer'], attr: Optional[Mapping[str, str]] = None, exclude: Sequence[str] = (), validate: bool = True, sign: bool = False, inner_layer: Optional['stem.descriptor.hidden_service.InnerLayer'] = None, revision_counter: Optional[int] = None, authorized_clients: Optional[Sequence['stem.descriptor.hidden_service.AuthorizedClient']] = None, subcredential: bytes = None, blinded_key: bytes = None) -> bytes:
    try:
      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
      from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    except ImportError:
      raise ImportError('Hidden service layer creation requires cryptography 2.6 or later')

    if authorized_clients and 'auth-client' in attr:
      raise ValueError('Authorized clients cannot be specified through both attr and authorized_clients')

    inner_layer = inner_layer if inner_layer else InnerLayer.create()
    revision_counter = revision_counter if revision_counter else 1
    blinded_key = blinded_key if blinded_key else stem.util._pubkey_bytes(Ed25519PrivateKey.generate())
    subcredential = subcredential if subcredential else HiddenServiceDescriptorV3._subcredential(Ed25519PrivateKey.generate(), blinded_key)

    if not authorized_clients:
      authorized_clients = []

      if not attr or 'auth-client' not in attr:
        authorized_clients.extend(AuthorizedClient() for _ in range(16))
    return _descriptor_content(
        attr,
        exclude,
        ([
            ('desc-auth-type', 'x25519'),
            (
                'desc-auth-ephemeral-key',
                stem.util.str_tools._to_unicode(
                    base64.b64encode(
                        stem.util._pubkey_bytes(X25519PrivateKey.generate()))),
            ),
        ] + [('auth-client', f'{c.id} {c.iv} {c.cookie}')
             for c in authorized_clients]),
        ((
            'encrypted',
            stem.util.str_tools._to_unicode(b'\n' + inner_layer._encrypt(
                revision_counter, subcredential, blinded_key)),
        ), ),
    )

  @classmethod
  def create(cls: Type['stem.descriptor.hidden_service.OuterLayer'], attr: Optional[Mapping[str, str]] = None, exclude: Sequence[str] = (), validate: bool = True, sign: bool = False, inner_layer: Optional['stem.descriptor.hidden_service.InnerLayer'] = None, revision_counter: int = None, authorized_clients: Optional[Sequence['stem.descriptor.hidden_service.AuthorizedClient']] = None, subcredential: bytes = None, blinded_key: bytes = None) -> 'stem.descriptor.hidden_service.OuterLayer':
    return cls(cls.content(attr, exclude, validate, sign, inner_layer, revision_counter, authorized_clients, subcredential, blinded_key), validate = validate)

  def __init__(self, content: bytes, validate: bool = False) -> None:
    content = stem.util.str_tools._to_bytes(content).rstrip(b'\x00')  # strip null byte padding

    super(OuterLayer, self).__init__(content, lazy_load = not validate)
    entries = _descriptor_components(content, validate)

    if validate:
      self._parse(entries, validate)
    else:
      self._entries = entries


class InnerLayer(Descriptor):
  """
  Second encryped layer of a hidden service v3 descriptor (`spec
  <https://gitweb.torproject.org/torspec.git/tree/rend-spec-v3.txt>`_ section
  2.5.2.2).

  .. versionadded:: 1.8.0

  :var stem.descriptor.hidden_service.OuterLayer outer: enclosing encryption layer

  :var list formats: **\\*** recognized CREATE2 cell formats
  :var list intro_auth: **\\*** introduction-layer authentication types
  :var bool is_single_service: **\\*** **True** if this is a `single onion service <https://gitweb.torproject.org/torspec.git/tree/proposals/260-rend-single-onion.txt>`_, **False** otherwise
  :var list introduction_points: :class:`~stem.descriptor.hidden_service.IntroductionPointV3` where this service is reachable

  **\\*** attribute is either required when we're parsed with validation or has
  a default value, others are left as **None** if undefined
  """

  ATTRIBUTES = {
    'formats': ([], _parse_v3_inner_formats),
    'intro_auth': ([], _parse_v3_inner_intro_auth),
    'is_single_service': (False, _parse_v3_inner_single_service),
    'introduction_points': ([], _parse_v3_introduction_points),
  }

  PARSER_FOR_LINE = {
    'create2-formats': _parse_v3_inner_formats,
    'intro-auth-required': _parse_v3_inner_intro_auth,
    'single-onion-service': _parse_v3_inner_single_service,
  }

  @staticmethod
  def _decrypt(outer_layer: 'stem.descriptor.hidden_service.OuterLayer', revision_counter: int, subcredential: bytes, blinded_key: bytes) -> 'stem.descriptor.hidden_service.InnerLayer':
    plaintext = _decrypt_layer(outer_layer.encrypted, b'hsdir-encrypted-data', revision_counter, subcredential, blinded_key)
    return InnerLayer(stem.util.str_tools._to_bytes(plaintext), validate = True, outer_layer = outer_layer)

  def _encrypt(self, revision_counter: int, subcredential: bytes, blinded_key: bytes) -> bytes:
    # encrypt back into an outer layer's 'encrypted' field

    return _encrypt_layer(self.get_bytes(), b'hsdir-encrypted-data', revision_counter, subcredential, blinded_key)

  @classmethod
  def content(cls: Type['stem.descriptor.hidden_service.InnerLayer'], attr: Optional[Mapping[str, str]] = None, exclude: Sequence[str] = (), introduction_points: Optional[Sequence['stem.descriptor.hidden_service.IntroductionPointV3']] = None) -> bytes:
    if introduction_points:
      suffix = '\n' + '\n'.join(map(IntroductionPointV3.encode, introduction_points)) + '\n'
    else:
      suffix = '\n'

    return _descriptor_content(attr, exclude, (
      ('create2-formats', '2'),
    )) + stem.util.str_tools._to_bytes(suffix)

  @classmethod
  def create(cls: Type['stem.descriptor.hidden_service.InnerLayer'], attr: Optional[Mapping[str, str]] = None, exclude: Sequence[str] = (), validate: bool = True, introduction_points: Optional[Sequence['stem.descriptor.hidden_service.IntroductionPointV3']] = None) -> 'stem.descriptor.hidden_service.InnerLayer':
    return cls(cls.content(attr, exclude, introduction_points), validate = validate)

  def __init__(self, content: bytes, validate: bool = False, outer_layer: Optional['stem.descriptor.hidden_service.OuterLayer'] = None) -> None:
    super(InnerLayer, self).__init__(content, lazy_load = not validate)
    self.outer = outer_layer

    # inner layer begins with a few header fields, followed by any
    # number of introduction-points

    content = stem.util.str_tools._to_bytes(content)
    div = content.find(b'\nintroduction-point ')

    if div != -1:
      self._unparsed_introduction_points = content[div + 1:]
      content = content[:div]
    else:
      self._unparsed_introduction_points = None

    entries = _descriptor_components(content, validate)

    if validate:
      self._parse(entries, validate)
      _parse_v3_introduction_points(self, entries)
    else:
      self._entries = entries


def _blinded_pubkey(identity_key: bytes, blinding_nonce: bytes) -> bytes:
  from stem.util import ed25519

  mult = 2 ** (ed25519.b - 2) + sum(2 ** i * ed25519.bit(blinding_nonce, i) for i in range(3, ed25519.b - 2))
  P = ed25519.decodepoint(stem.util._pubkey_bytes(identity_key))
  return ed25519.encodepoint(ed25519.scalarmult(P, mult))


def _blinded_sign(msg: bytes, identity_key: 'cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey', blinded_key: bytes, blinding_nonce: bytes) -> bytes:  # type: ignore
  try:
    from cryptography.hazmat.primitives import serialization
  except ImportError:
    raise ImportError('Key signing requires the cryptography module')

  from stem.util import ed25519

  identity_key_bytes = identity_key.private_bytes(
    encoding = serialization.Encoding.Raw,
    format = serialization.PrivateFormat.Raw,
    encryption_algorithm = serialization.NoEncryption(),
  )

  # pad private identity key into an ESK (encrypted secret key)

  h = ed25519.H(identity_key_bytes)
  a = 2 ** (ed25519.b - 2) + sum(2 ** i * ed25519.bit(h, i) for i in range(3, ed25519.b - 2))
  k = b''.join([h[i:i + 1] for i in range(ed25519.b // 8, ed25519.b // 4)])
  esk = ed25519.encodeint(a) + k

  # blind the ESK with this nonce

  mult = 2 ** (ed25519.b - 2) + sum(2 ** i * ed25519.bit(blinding_nonce, i) for i in range(3, ed25519.b - 2))
  s = ed25519.decodeint(esk[:32])
  s_prime = (s * mult) % ed25519.l
  k = esk[32:]
  k_prime = ed25519.H(b'Derive temporary signing key hash input' + k)[:32]
  blinded_esk = ed25519.encodeint(s_prime) + k_prime

  # finally, sign the message

  a = ed25519.decodeint(blinded_esk[:32])
  r = ed25519.Hint(b''.join([blinded_esk[i:i + 1] for i in range(ed25519.b // 8, ed25519.b // 4)]) + msg)
  R = ed25519.scalarmult(ed25519.B, r)
  S = (r + ed25519.Hint(ed25519.encodepoint(R) + blinded_key + msg) * a) % ed25519.l

  return ed25519.encodepoint(R) + ed25519.encodeint(S)
