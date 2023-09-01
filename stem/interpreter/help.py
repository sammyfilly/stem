# Copyright 2014-2020, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Provides our /help responses.
"""

import functools
from typing import cast

import stem.control
import stem.util.conf

from stem.util.term import format

from stem.interpreter import (
  STANDARD_OUTPUT,
  BOLD_OUTPUT,
  ERROR_OUTPUT,
  msg,
  uses_settings,
)


def response(controller: stem.control.Controller, arg: str) -> str:
  """
  Provides our /help response.

  :param controller: tor control connection
  :param arg: controller or interpreter command to provide help output for

  :returns: **str** with our help response
  """

  # Normalizing inputs first so we can better cache responses.

  return _response(controller, _normalize(arg))


def _normalize(arg: str) -> str:
  arg = arg.upper()

  # If there's multiple arguments then just take the first. This is
  # particularly likely if they're trying to query a full command (for
  # instance "/help GETINFO version")

  arg = arg.split(' ')[0]

  # strip slash if someone enters an interpreter command (ex. "/help /help")

  if arg.startswith('/'):
    arg = arg[1:]

  return arg


@functools.lru_cache()
@uses_settings
def _response(controller: stem.control.Controller, arg: str, config: stem.util.conf.Config) -> str:
  if not arg:
    return _general_help()

  usage_info = config.get('help.usage', {})

  if arg not in usage_info:
    return format(f"No help information available for '{arg}'...", *ERROR_OUTPUT)

  output = format(usage_info[arg] + '\n', *BOLD_OUTPUT)

  description = config.get(f'help.description.{arg.lower()}', '')

  for line in description.splitlines():
    output += format(f'  {line}', *STANDARD_OUTPUT) + '\n'

  output += '\n'

  if arg == 'GETINFO':
    if results := cast(str, controller.get_info('info/names', None)):
      for line in results.splitlines():
        if ' -- ' in line:
          opt, summary = line.split(' -- ', 1)

          output += format('%-33s' % opt, *BOLD_OUTPUT)
          output += format(f' - {summary}', *STANDARD_OUTPUT) + '\n'
  elif arg == 'GETCONF':
    if results := cast(str, controller.get_info('config/names', None)):
      options = [opt.split(' ', 1)[0] for opt in results.splitlines()]

      for i in range(0, len(options), 2):
        line = ''.join('%-42s' % entry for entry in options[i:i + 2])
        output += format(line.rstrip(), *STANDARD_OUTPUT) + '\n'
  elif arg == 'SIGNAL':
    signal_options = config.get('help.signal.options', {})

    for signal, summary in signal_options.items():
      output += format('%-15s' % signal, *BOLD_OUTPUT)
      output += format(f' - {summary}', *STANDARD_OUTPUT) + '\n'
  elif arg == 'SETEVENTS':
    if results := cast(str, controller.get_info('events/names', None)):
      entries = results.split()

      # displays four columns of 20 characters

      for i in range(0, len(entries), 4):
        line = ''.join('%-20s' % entry for entry in entries[i:i + 4])
        output += format(line.rstrip(), *STANDARD_OUTPUT) + '\n'
  elif arg == 'USEFEATURE':
    if results := cast(str, controller.get_info('features/names', None)):
      output += format(results, *STANDARD_OUTPUT) + '\n'
  elif arg in {'LOADCONF', 'POSTDESCRIPTOR'}:
    # gives a warning that this option isn't yet implemented
    output += format(msg('msg.multiline_unimplemented_notice'), *ERROR_OUTPUT) + '\n'

  return output.rstrip()


def _general_help() -> str:
  lines = []

  for line in msg('help.general').splitlines():
    div = line.find(' - ')

    if div != -1:
      cmd, description = line[:div], line[div:]
      lines.append(format(cmd, *BOLD_OUTPUT) + format(description, *STANDARD_OUTPUT))
    else:
      lines.append(format(line, *BOLD_OUTPUT))

  return '\n'.join(lines)
