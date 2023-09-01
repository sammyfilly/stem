# Copyright 2011-2020, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Helper functions for querying process and system information from the /proc
contents. Fetching information this way provides huge performance benefits
over lookups via system utilities (ps, netstat, etc). For instance, resolving
connections this way cuts the runtime by around 90% verses the alternatives.
These functions may not work on all platforms (only Linux?).

The method for reading these files (and a little code) are borrowed from
`psutil <https://code.google.com/p/psutil/>`_, which was written by Jay Loden,
Dave Daeschler, Giampaolo Rodola' and is under the BSD license.

**These functions are not being vended to stem users. They may change in the
future, use them at your own risk.**

**Module Overview:**

::

  is_available - checks if proc utilities can be used on this system
  system_start_time - unix timestamp for when the system started
  physical_memory - memory available on this system
  cwd - provides the current working directory for a process
  uid - provides the user id a process is running under
  memory_usage - provides the memory usage of a process
  stats - queries statistics about a process
  file_descriptors_used - number of file descriptors used by a process
  connections - provides the connections made by a process

.. data:: Stat (enum)

  Types of data available via the :func:`~stem.util.proc.stats` function.

  ============== ===========
  Stat           Description
  ============== ===========
  **COMMAND**    command name under which the process is running
  **CPU_UTIME**  total user time spent on the process
  **CPU_STIME**  total system time spent on the process
  **START_TIME** when this process began, in unix time
  ============== ===========
"""

import base64
import functools
import os
import platform
import socket
import sys
import time

import stem.util.connection
import stem.util.enum
import stem.util.str_tools

from stem.util import log
from typing import Any, Mapping, Optional, Sequence, Set, Tuple

try:
  # unavailable on windows (#19823)
  import pwd
  IS_PWD_AVAILABLE = True
except ImportError:
  IS_PWD_AVAILABLE = False

# os.sysconf is only defined on unix
try:
  CLOCK_TICKS = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
except AttributeError:
  CLOCK_TICKS = None

IS_LITTLE_ENDIAN = sys.byteorder == 'little'
ENCODED_ADDR = {}  # cache of encoded ips to their decoded version

Stat = stem.util.enum.Enum(
  ('COMMAND', 'command'), ('CPU_UTIME', 'utime'),
  ('CPU_STIME', 'stime'), ('START_TIME', 'start time')
)


@functools.lru_cache()
def is_available() -> bool:
  """
  Checks if proc information is available on this platform.

  :returns: **True** if proc contents exist on this platform, **False** otherwise
  """

  if platform.system() != 'Linux':
    return False
  # list of process independent proc paths we use
  proc_paths = ('/proc/stat', '/proc/meminfo', '/proc/net/tcp', '/proc/net/udp')

  return all(os.path.exists(path) for path in proc_paths)


@functools.lru_cache()
def system_start_time() -> float:
  """
  Provides the unix time (seconds since epoch) when the system started.

  :returns: **float** for the unix time of when the system started

  :raises: **OSError** if it can't be determined
  """

  start_time, parameter = time.time(), 'system start time'
  btime_line = _get_line('/proc/stat', 'btime', parameter)

  try:
    result = float(btime_line.strip().split()[1])
    _log_runtime(parameter, '/proc/stat[btime]', start_time)
    return result
  except:
    exc = OSError(f'unable to parse the /proc/stat btime entry: {btime_line}')
    _log_failure(parameter, exc)
    raise exc


@functools.lru_cache()
def physical_memory() -> int:
  """
  Provides the total physical memory on the system in bytes.

  :returns: **int** for the bytes of physical memory this system has

  :raises: **OSError** if it can't be determined
  """

  start_time, parameter = time.time(), 'system physical memory'
  mem_total_line = _get_line('/proc/meminfo', 'MemTotal:', parameter)

  try:
    result = int(mem_total_line.split()[1]) * 1024
    _log_runtime(parameter, '/proc/meminfo[MemTotal]', start_time)
    return result
  except:
    exc = OSError(
        f'unable to parse the /proc/meminfo MemTotal entry: {mem_total_line}')
    _log_failure(parameter, exc)
    raise exc


def cwd(pid: int) -> str:
  """
  Provides the current working directory for the given process.

  :param pid: process id of the process to be queried

  :returns: **str** with the path of the working directory for the process

  :raises: **OSError** if it can't be determined
  """

  start_time, parameter = time.time(), 'cwd'
  proc_cwd_link = f'/proc/{pid}/cwd'

  if pid == 0:
    cwd = ''
  else:
    try:
      cwd = os.readlink(proc_cwd_link)
    except OSError:
      exc = OSError(f'unable to read {proc_cwd_link}')
      _log_failure(parameter, exc)
      raise exc

  _log_runtime(parameter, proc_cwd_link, start_time)
  return cwd


def uid(pid: int) -> int:
  """
  Provides the user ID the given process is running under.

  :param pid: process id of the process to be queried

  :returns: **int** with the user id for the owner of the process

  :raises: **OSError** if it can't be determined
  """

  start_time, parameter = time.time(), 'uid'
  status_path = f'/proc/{pid}/status'
  uid_line = _get_line(status_path, 'Uid:', parameter)

  try:
    result = int(uid_line.split()[1])
    _log_runtime(parameter, f'{status_path}[Uid]', start_time)
    return result
  except:
    exc = OSError(f'unable to parse the {status_path} Uid entry: {uid_line}')
    _log_failure(parameter, exc)
    raise exc


def memory_usage(pid: int) -> Tuple[int, int]:
  """
  Provides the memory usage in bytes for the given process.

  :param pid: process id of the process to be queried

  :returns: **tuple** of two ints with the memory usage of the process, of the
    form **(resident_size, virtual_size)**

  :raises: **OSError** if it can't be determined
  """

  # checks if this is the kernel process

  if pid == 0:
    return (0, 0)

  start_time, parameter = time.time(), 'memory usage'
  status_path = f'/proc/{pid}/status'
  mem_lines = _get_lines(status_path, ('VmRSS:', 'VmSize:'), parameter)

  try:
    residentSize = int(mem_lines['VmRSS:'].split()[1]) * 1024
    virtualSize = int(mem_lines['VmSize:'].split()[1]) * 1024

    _log_runtime(parameter, f'{status_path}[VmRSS|VmSize]', start_time)
    return (residentSize, virtualSize)
  except:
    exc = OSError(
        f"unable to parse the {status_path} VmRSS and VmSize entries: {', '.join(mem_lines)}"
    )
    _log_failure(parameter, exc)
    raise exc


def stats(pid: int, *stat_types: 'stem.util.proc.Stat') -> Sequence[str]:
  """
  Provides process specific information. See the :data:`~stem.util.proc.Stat`
  enum for valid options.

  :param pid: process id of the process to be queried
  :param stat_types: information to be provided back

  :returns: **tuple** with all of the requested statistics as strings

  :raises: **OSError** if it can't be determined
  """

  if CLOCK_TICKS is None:
    raise OSError('Unable to look up SC_CLK_TCK')

  start_time, parameter = time.time(), f"process {', '.join(stat_types)}"

  # the stat file contains a single line, of the form...
  # 8438 (tor) S 8407 8438 8407 34818 8438 4202496...
  stat_path = f'/proc/{pid}/stat'
  stat_line = _get_line(stat_path, str(pid), parameter)

  # breaks line into component values
  stat_comp = []
  cmd_start, cmd_end = stat_line.find('('), stat_line.find(')')

  if cmd_start != -1 and cmd_end != -1:
    stat_comp.extend((stat_line[:cmd_start], stat_line[cmd_start + 1:cmd_end]))
    stat_comp += stat_line[cmd_end + 1:].split()

  if len(stat_comp) < 44 and _is_float(stat_comp[13], stat_comp[14], stat_comp[21]):
    exc = OSError(f'stat file had an unexpected format: {stat_path}')
    _log_failure(parameter, exc)
    raise exc

  results = []

  for stat_type in stat_types:
    if stat_type == Stat.COMMAND:
      if pid == 0:
        results.append('sched')
      else:
        results.append(stat_comp[1])
    elif stat_type == Stat.CPU_UTIME:
      if pid == 0:
        results.append('0')
      else:
        results.append(str(float(stat_comp[13]) / CLOCK_TICKS))
    elif stat_type == Stat.CPU_STIME:
      if pid == 0:
        results.append('0')
      else:
        results.append(str(float(stat_comp[14]) / CLOCK_TICKS))
    elif stat_type == Stat.START_TIME:
      if pid == 0:
        results.append(str(system_start_time()))
      else:
        # According to documentation, starttime is in field 21 and the unit is
        # jiffies (clock ticks). We divide it for clock ticks, then add the
        # uptime to get the seconds since the epoch.
        p_start_time = float(stat_comp[21]) / CLOCK_TICKS
        results.append(str(p_start_time + system_start_time()))

  _log_runtime(parameter, stat_path, start_time)
  return tuple(results)


def file_descriptors_used(pid: int) -> int:
  """
  Provides the number of file descriptors currently being used by a process.

  .. versionadded:: 1.3.0

  :param pid: process id of the process to be queried

  :returns: **int** of the number of file descriptors used

  :raises: **OSError** if it can't be determined
  """

  try:
    pid = pid

    if pid < 0:
      raise OSError(f"Process pids can't be negative: {pid}")
  except (ValueError, TypeError):
    raise OSError(f'Process pid was non-numeric: {pid}')

  try:
    return len(os.listdir('/proc/%i/fd' % pid))
  except Exception as exc:
    raise OSError(f'Unable to check number of file descriptors used: {exc}')


def connections(pid: Optional[int] = None, user: Optional[str] = None) -> Sequence['stem.util.connection.Connection']:
  """
  Queries connections from the proc contents. This matches netstat, lsof, and
  friends but is much faster. If no **pid** or **user** are provided this
  provides all present connections.

  :param pid: pid to provide connections for
  :param user: username to look up connections for

  :returns: **list** of :class:`~stem.util.connection.Connection` instances

  :raises: **OSError** if it can't be determined
  """

  start_time, conn = time.time(), []

  if pid:
    parameter = f'connections for pid {pid}'

    try:
      pid = int(pid)

      if pid < 0:
        raise OSError(f"Process pids can't be negative: {pid}")
    except (ValueError, TypeError):
      raise OSError(f'Process pid was non-numeric: {pid}')
  elif user:
    parameter = f'connections for user {user}'
  else:
    parameter = 'all connections'

  try:
    if not IS_PWD_AVAILABLE:
      raise OSError("This requires python's pwd module, which is unavailable on Windows.")

    inodes = _inodes_for_sockets(pid) if pid else set()
    process_uid = stem.util.str_tools._to_bytes(str(pwd.getpwnam(user).pw_uid)) if user else None

    for proc_file_path in ('/proc/net/tcp', '/proc/net/tcp6', '/proc/net/udp', '/proc/net/udp6'):
      if proc_file_path.endswith('6') and not os.path.exists(proc_file_path):
        continue  # ipv6 proc contents are optional

      protocol = proc_file_path[10:].rstrip('6')  # 'tcp' or 'udp'
      is_ipv6 = proc_file_path.endswith('6')

      try:
        with open(proc_file_path, 'rb') as proc_file:
          proc_file.readline()  # skip the first line

          for line in proc_file:
            _, l_dst, r_dst, status, _, _, _, uid, _, inode = line.split()[:10]

            if (inodes and inode not in inodes
                or process_uid and uid != process_uid
                or protocol == 'tcp' and status != b'01'):
              continue
            div = l_dst.find(b':')
            l_addr = _unpack_addr(l_dst[:div])
            l_port = int(l_dst[div + 1:], 16)

            div = r_dst.find(b':')
            r_addr = _unpack_addr(r_dst[:div])
            r_port = int(r_dst[div + 1:], 16)

            if (r_addr in ['0.0.0.0', '0000:0000:0000:0000:0000:0000']
                or l_port == 0 or r_port == 0):
              continue  # no address
            conn.append(stem.util.connection.Connection(l_addr, l_port, r_addr, r_port, protocol, is_ipv6))
      except OSError as exc:
        raise OSError(f"unable to read '{proc_file_path}': {exc}")
      except Exception as exc:
        raise OSError(f"unable to parse '{proc_file_path}': {exc}")

    _log_runtime(parameter, '/proc/net/[tcp|udp]', start_time)
    return conn
  except OSError as exc:
    _log_failure(parameter, exc)
    raise


def _inodes_for_sockets(pid: int) -> Set[bytes]:
  """
  Provides inodes in use by a process for its sockets.

  :param pid: process id of the process to be queried

  :returns: **set** with inodes for its sockets

  :raises: **OSError** if it can't be determined
  """

  inodes = set()

  try:
    fd_contents = os.listdir(f'/proc/{pid}/fd')
  except OSError as exc:
    raise OSError(f'Unable to read our file descriptors: {exc}')

  for fd in fd_contents:
    fd_path = f'/proc/{pid}/fd/{fd}'

    try:
      # File descriptor link, such as 'socket:[30899]'

      fd_name = os.readlink(fd_path)

      if fd_name.startswith('socket:['):
        inodes.add(stem.util.str_tools._to_bytes(fd_name[8:-1]))
    except OSError as exc:
      if not os.path.exists(fd_path):
        continue  # descriptors may shift while we're in the middle of iterating over them

      # most likely couldn't be read due to permissions
      raise OSError(
          f'unable to determine file descriptor destination ({exc}): {fd_path}'
      )

  return inodes


def _unpack_addr(addr: bytes) -> str:
  """
  Translates an address entry in the /proc/net/* contents to a human readable
  form (`reference <http://linuxdevcenter.com/pub/a/linux/2000/11/16/LinuxAdmin.html>`_,
  for instance:

  ::

    "0500000A" -> "10.0.0.5"
    "F804012A4A5190010000000002000000" -> "2a01:4f8:190:514a::2"

  :param addr: proc address entry to be decoded

  :returns: **str** of the decoded address
  """

  if addr not in ENCODED_ADDR:
    if len(addr) == 8:
      # IPv4 address
      decoded = base64.b16decode(addr)[::-1] if IS_LITTLE_ENDIAN else base64.b16decode(addr)
      ENCODED_ADDR[addr] = socket.inet_ntop(socket.AF_INET, decoded)
    else:
      # IPv6 address

      if IS_LITTLE_ENDIAN:
        # group into eight characters, then invert in pairs

        inverted = []

        for i in range(4):
          grouping = addr[8 * i:8 * (i + 1)]
          inverted += [grouping[2 * i:2 * (i + 1)] for i in range(4)][::-1]

        encoded = b''.join(inverted)
      else:
        encoded = addr

      ENCODED_ADDR[addr] = stem.util.connection.expand_ipv6_address(socket.inet_ntop(socket.AF_INET6, base64.b16decode(encoded)))

  return ENCODED_ADDR[addr]


def _is_float(*value: Any) -> bool:
  try:
    for v in value:
      float(v)

    return True
  except ValueError:
    return False


def _get_line(file_path: str, line_prefix: str, parameter: str) -> str:
  return _get_lines(file_path, (line_prefix, ), parameter)[line_prefix]


def _get_lines(file_path: str, line_prefixes: Sequence[str], parameter: str) -> Mapping[str, str]:
  """
  Fetches lines with the given prefixes from a file. This only provides back
  the first instance of each prefix.

  :param file_path: path of the file to read
  :param line_prefixes: string prefixes of the lines to return
  :param parameter: description of the proc attribute being fetch

  :returns: mapping of prefixes to the matching line

  :raises: **OSError** if unable to read the file or can't find all of the prefixes
  """

  try:
    remaining_prefixes = list(line_prefixes)
    proc_file, results = open(file_path), {}

    for line in proc_file:
      if not remaining_prefixes:
        break  # found everything we're looking for

      for prefix in remaining_prefixes:
        if line.startswith(prefix):
          results[prefix] = line
          remaining_prefixes.remove(prefix)
          break

    proc_file.close()

    if not remaining_prefixes:
      return results
    msg = (
        f'{file_path} did not contain a {remaining_prefixes[0]} entry'
        if len(remaining_prefixes) == 1 else
        f"{file_path} did not contain {', '.join(remaining_prefixes)} entries")
    raise OSError(msg)
  except OSError as exc:
    _log_failure(parameter, exc)
    raise


def _log_runtime(parameter: str, proc_location: str, start_time: float) -> None:
  """
  Logs a message indicating a successful proc query.

  :param parameter: description of the proc attribute being fetch
  :param proc_location: proc files we were querying
  :param start_time: unix time for when this query was started
  """

  runtime = time.time() - start_time
  log.debug('proc call (%s): %s (runtime: %0.4f)' % (parameter, proc_location, runtime))


def _log_failure(parameter: str, exc: BaseException) -> None:
  """
  Logs a message indicating that the proc query failed.

  :param parameter: description of the proc attribute being fetch
  :param exc: exception that we're raising
  """

  log.debug(f'proc call failed ({parameter}): {exc}')
