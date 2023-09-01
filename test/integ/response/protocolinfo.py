"""
Integration tests for the stem.response.protocolinfo.ProtocolInfoResponse class
and related functions.
"""

import unittest

import stem.connection
import stem.socket
import stem.util.system
import stem.version
import test
import test.integ.util.system
import test.require
import test.runner
from stem.util.test_tools import async_test

from unittest.mock import Mock, patch


class TestProtocolInfo(unittest.TestCase):
  @test.require.controller
  @async_test
  async def test_parsing(self):
    """
    Makes a PROTOCOLINFO query and processes the response for our control
    connection.
    """

    control_socket = await test.runner.get_runner().get_tor_socket(False)
    await control_socket.send('PROTOCOLINFO 1')
    protocolinfo_response = await control_socket.recv()
    stem.response.convert('PROTOCOLINFO', protocolinfo_response)
    await control_socket.close()

    # according to the control spec the following _could_ differ or be
    # undefined but if that actually happens then it's gonna make people sad

    self.assertEqual(1, protocolinfo_response.protocol_version)
    self.assertNotEqual(None, protocolinfo_response.tor_version)
    self.assertNotEqual(None, protocolinfo_response.auth_methods)

    self.assert_matches_test_config(protocolinfo_response)

  @test.require.controller
  @patch('stem.util.proc.is_available', Mock(return_value = False))
  @patch('stem.util.system.is_available', Mock(return_value = True))
  @async_test
  async def test_get_protocolinfo_path_expansion(self):
    """
    If we're running with the 'RELATIVE' target then test_parsing() will
    exercise cookie path expansion when we're able to query the pid by our
    prcess name. This test selectively disables system.call() so we exercise
    the expansion via our control port or socket file.

    This test is largely redundant with test_parsing() if we aren't running
    with the 'RELATIVE' target.
    """

    if test.runner.Torrc.PORT in test.runner.get_runner().get_options():
      lookup_prefixes = (
        stem.util.system.GET_PID_BY_PORT_NETSTAT,
        stem.util.system.GET_PID_BY_PORT_SOCKSTAT % '',
        stem.util.system.GET_PID_BY_PORT_LSOF,
        stem.util.system.GET_CWD_PWDX % '',
        'lsof -a -p ')

      control_socket = stem.socket.ControlPort(port = test.runner.CONTROL_PORT)
    else:
      lookup_prefixes = (
        stem.util.system.GET_PID_BY_FILE_LSOF % '',
        stem.util.system.GET_CWD_PWDX % '',
        'lsof -a -p ')

      control_socket = stem.socket.ControlSocketFile(test.runner.CONTROL_SOCKET_PATH)

    await control_socket.connect()

    call_replacement = test.integ.util.system.filter_system_call(lookup_prefixes)

    with patch('stem.util.system.call') as call_mock:
      call_mock.side_effect = call_replacement

      protocolinfo_response = await stem.connection.get_protocolinfo(control_socket)
      self.assert_matches_test_config(protocolinfo_response)

      # we should have a usable socket at this point
      self.assertTrue(control_socket.is_alive())
      await control_socket.close()

  @test.require.controller
  @async_test
  async def test_multiple_protocolinfo_calls(self):
    """
    Tests making repeated PROTOCOLINFO queries. This use case is interesting
    because tor will shut down the socket and stem should transparently
    re-establish it.
    """

    async with await test.runner.get_runner().get_tor_socket(False) as control_socket:
      for _ in range(5):
        protocolinfo_response = await stem.connection.get_protocolinfo(control_socket)
        self.assert_matches_test_config(protocolinfo_response)

  @test.require.controller
  @async_test
  async def test_pre_disconnected_query(self):
    """
    Tests making a PROTOCOLINFO query when previous use of the socket had
    already disconnected it.
    """

    async with await test.runner.get_runner().get_tor_socket(False) as control_socket:
      # makes a couple protocolinfo queries outside of get_protocolinfo first
      await control_socket.send('PROTOCOLINFO 1')
      await control_socket.recv()

      await control_socket.send('PROTOCOLINFO 1')
      await control_socket.recv()

      protocolinfo_response = await stem.connection.get_protocolinfo(control_socket)
      self.assert_matches_test_config(protocolinfo_response)

  def assert_matches_test_config(self, protocolinfo_response):
    """
    Makes assertions that the protocolinfo response's attributes match those of
    the test configuration.
    """

    runner = test.runner.get_runner()
    tor_options = runner.get_options()
    auth_methods, auth_cookie_path = [], None

    if test.runner.Torrc.COOKIE in tor_options:
      auth_methods.extend((
          stem.connection.AuthMethod.COOKIE,
          stem.connection.AuthMethod.SAFECOOKIE,
      ))
      chroot_path = runner.get_chroot()
      auth_cookie_path = runner.get_auth_cookie_path()

      if chroot_path and auth_cookie_path.startswith(chroot_path):
        auth_cookie_path = auth_cookie_path[len(chroot_path):]

    if test.runner.Torrc.PASSWORD in tor_options:
      auth_methods.append(stem.connection.AuthMethod.PASSWORD)

    if not auth_methods:
      auth_methods.append(stem.connection.AuthMethod.NONE)

    self.assertEqual((), protocolinfo_response.unknown_auth_methods)
    self.assertEqual(tuple(auth_methods), protocolinfo_response.auth_methods)
    self.assertEqual(auth_cookie_path, protocolinfo_response.cookie_path)
