"""
Unit tests for the stem.connection.connect function.
"""

import io
import unittest

import stem
import stem.connection
import stem.socket

from unittest.mock import Mock, patch

from stem.util.test_tools import (
  async_test,
  coro_func_raising_exc,
  coro_func_returning_value,
)


class TestConnect(unittest.TestCase):
  @patch('sys.stdout', new_callable = io.StringIO)
  @patch('stem.util.system.is_running')
  @patch('os.path.exists', Mock(return_value = True))
  @patch('stem.socket.ControlSocketFile', Mock(side_effect = stem.SocketError('failed')))
  @patch('stem.socket.ControlPort', Mock(side_effect = stem.SocketError('failed')))
  @patch('stem.connection._connect_auth', Mock(side_effect = coro_func_returning_value(None)))
  def test_failue_with_the_default_endpoint(self, is_running_mock, stdout_mock):
    is_running_mock.return_value = False
    self._assert_connect_fails_with({}, stdout_mock, "Unable to connect to tor. Are you sure it's running?")

    is_running_mock.return_value = True
    self._assert_connect_fails_with({}, stdout_mock, "Unable to connect to tor. Maybe it's running without a ControlPort?")

  @patch('sys.stdout', new_callable = io.StringIO)
  @patch('os.path.exists')
  @patch('stem.util.system.is_running', Mock(return_value = True))
  @patch('stem.socket.ControlSocketFile', Mock(side_effect = stem.SocketError('failed')))
  @patch('stem.socket.ControlPort', Mock(side_effect = stem.SocketError('failed')))
  @patch('stem.connection._connect_auth', Mock(side_effect = coro_func_returning_value(None)))
  def test_failure_with_a_custom_endpoint(self, path_exists_mock, stdout_mock):
    path_exists_mock.return_value = True
    self._assert_connect_fails_with({'control_port': ('127.0.0.1', 80), 'control_socket': None}, stdout_mock, "Unable to connect to 127.0.0.1:80: failed")
    self._assert_connect_fails_with({'control_port': None, 'control_socket': '/tmp/my_socket'}, stdout_mock, "Unable to connect to '/tmp/my_socket': failed")

    path_exists_mock.return_value = False
    self._assert_connect_fails_with({'control_port': ('127.0.0.1', 80), 'control_socket': None}, stdout_mock, "Unable to connect to 127.0.0.1:80: failed")
    self._assert_connect_fails_with({'control_port': None, 'control_socket': '/tmp/my_socket'}, stdout_mock, "The socket file you specified (/tmp/my_socket) doesn't exist")

  @patch('stem.socket.ControlPort')
  @patch('os.path.exists', Mock(return_value = False))
  @patch('stem.connection._connect_auth', Mock(side_effect = coro_func_returning_value(None)))
  def test_getting_a_control_port(self, port_mock):
    port_connect_mock = port_mock.return_value.connect
    port_connect_mock.side_effect = coro_func_returning_value(None)
    stem.connection.connect()
    port_mock.assert_called_once_with('127.0.0.1', 9051)
    port_connect_mock.assert_called_once()
    port_mock.reset_mock()

    stem.connection.connect(control_port = ('255.0.0.10', 80), control_socket = None)
    port_mock.assert_called_once_with('255.0.0.10', 80)

  @patch('stem.socket.ControlSocketFile')
  @patch('os.path.exists', Mock(return_value = True))
  @patch('stem.connection._connect_auth', Mock(side_effect = coro_func_returning_value(None)))
  def test_getting_a_control_socket(self, socket_mock):
    socket_connect_mock = socket_mock.return_value.connect
    socket_connect_mock.side_effect = coro_func_returning_value(None)
    stem.connection.connect()
    socket_mock.assert_called_once_with('/var/run/tor/control')
    socket_connect_mock.assert_called_once()
    socket_mock.reset_mock()

    stem.connection.connect(control_port = None, control_socket = '/tmp/my_socket')
    socket_mock.assert_called_once_with('/tmp/my_socket')

  def _assert_connect_fails_with(self, args, stdout_mock, msg):
    result = stem.connection.connect(**args)

    if result is not None:
      self.fail()

    # Python 3.x seems to have an oddity where StringIO has prefixed null
    # characters (\x00) after we call truncate(). This could be addressed
    # a couple ways...
    #
    #   * Don't use a stdout mock more than once.
    #   * Strip the null characters.
    #
    # Opting for the second (which is admittedly a hack) so the tests are a
    # little nicer.

    stdout_output = stdout_mock.getvalue()
    stdout_mock.truncate(0)
    self.assertEqual(msg, stdout_output.strip().lstrip('\x00'))

  @patch('stem.connection.authenticate')
  @async_test
  async def test_auth_success(self, authenticate_mock):
    authenticate_mock.side_effect = coro_func_returning_value(None)
    control_socket = Mock()

    await stem.connection._connect_auth(control_socket, None, False, None, None)
    authenticate_mock.assert_called_with(control_socket, None, None)
    authenticate_mock.reset_mock()

    await stem.connection._connect_auth(control_socket, 's3krit!!!', False, '/my/chroot', None)
    authenticate_mock.assert_called_with(control_socket, 's3krit!!!', '/my/chroot')

  @patch('getpass.getpass')
  @patch('stem.connection.authenticate')
  @async_test
  async def test_auth_success_with_password_prompt(self, authenticate_mock, getpass_mock):
    control_socket = Mock()

    async def authenticate_mock_func(controller, password, *args):
      if password is None:
        raise stem.connection.MissingPassword('no password')
      elif password == 'my_password':
        return None  # success
      else:
        raise ValueError(f'Unexpected authenticate_mock input: {password}')

    authenticate_mock.side_effect = authenticate_mock_func
    getpass_mock.return_value = 'my_password'

    await stem.connection._connect_auth(control_socket, None, True, None, None)
    authenticate_mock.assert_any_call(control_socket, None, None)
    authenticate_mock.assert_any_call(control_socket, 'my_password', None)

  @patch('sys.stdout', new_callable = io.StringIO)
  @patch('stem.connection.authenticate')
  def test_auth_failure(self, authenticate_mock, stdout_mock):
    control_socket = stem.socket.ControlPort()

    authenticate_mock.side_effect = coro_func_raising_exc(stem.connection.IncorrectSocketType('unable to connect to socket'))
    self._assert_authenticate_fails_with(control_socket, stdout_mock, 'Please check in your torrc that 9051 is the ControlPort.')

    control_socket = stem.socket.ControlSocketFile()

    self._assert_authenticate_fails_with(control_socket, stdout_mock, 'Are you sure the interface you specified belongs to')

    authenticate_mock.side_effect = coro_func_raising_exc(stem.connection.UnrecognizedAuthMethods('unable to connect', ['telepathy']))
    self._assert_authenticate_fails_with(control_socket, stdout_mock, 'Tor is using a type of authentication we do not recognize...\n\n  telepathy')

    authenticate_mock.side_effect = coro_func_raising_exc(stem.connection.IncorrectPassword('password rejected'))
    self._assert_authenticate_fails_with(control_socket, stdout_mock, 'Incorrect password')

    authenticate_mock.side_effect = coro_func_raising_exc(stem.connection.UnreadableCookieFile('permission denied', '/tmp/my_cookie', False))
    self._assert_authenticate_fails_with(control_socket, stdout_mock, "We were unable to read tor's authentication cookie...\n\n  Path: /tmp/my_cookie\n  Issue: permission denied")

    authenticate_mock.side_effect = coro_func_raising_exc(stem.connection.OpenAuthRejected('crazy failure'))
    self._assert_authenticate_fails_with(control_socket, stdout_mock, 'Unable to authenticate: crazy failure')

  @async_test
  async def _assert_authenticate_fails_with(self, control_socket, stdout_mock, msg):
    result = await stem.connection._connect_auth(control_socket, None, False, None, None)

    if result is not None:
      self.fail()  # _connect_auth() was successful

    stdout_output = stdout_mock.getvalue()
    stdout_mock.truncate(0)

    if msg not in stdout_output:
      self.fail("Expected...\n\n%s\n\n... which couldn't be found in...\n\n%s" % (msg, stdout_output))
