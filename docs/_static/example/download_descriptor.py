"""
Simple script to dowload a descriptor from Tor's ORPort or DirPort.
"""

import collections
import getopt
import sys

import stem
import stem.descriptor.remote
import stem.util.connection
import stem.util.tor_tools

# By default downloading moria1's server descriptor from itself.

DEFAULT_ARGS = {
  'descriptor_type': 'server',
  'fingerprint': '9695DFC35FFEB861329B9F1AB04C46397020CE31',
  'download_from': stem.DirPort('128.31.0.34', 9131),
  'print_help': False,
}

VALID_TYPES = ('server', 'extrainfo', 'consensus')

HELP_TEXT = """\
Downloads a descriptor through Tor's ORPort or DirPort.

  -t, --type TYPE                 descriptor type to download, options are:
                                    %s
  -f, --fingerprint FP            relay to download the descriptor of
      --orport ADDRESS:PORT       ORPort to download from
      --dirport ADDRESS:PORT      DirPort to download from
  -h, --help                      presents this help
""" % ', '.join(VALID_TYPES)


def parse(argv):
  """
  Parses our arguments, providing a named tuple with their values.

  :param list argv: input arguments to be parsed

  :returns: a **named tuple** with our parsed arguments

  :raises: **ValueError** if we got an invalid argument
  """

  args = dict(DEFAULT_ARGS)

  try:
    recognized_args, unrecognized_args = getopt.getopt(argv, 't:f:h', ['type=', 'fingerprint=', 'orport=', 'dirport=', 'help'])

    if unrecognized_args:
      raise getopt.GetoptError(
          f"""'{"', '".join(unrecognized_args)}' aren't recognized arguments"""
      )
  except Exception as exc:
    raise ValueError(f'{exc} (for usage provide --help)')

  for opt, arg in recognized_args:
    if opt in ('-t', '--type'):
      args['descriptor_type'] = arg
    elif opt in ('-f', '--fingerprint'):
      if not stem.util.tor_tools.is_valid_fingerprint(arg):
        raise ValueError(f"'{arg}' isn't a relay fingerprint")

      args['fingerprint'] = arg
    elif opt in ('--orport', '--dirport'):
      if ':' not in arg:
        raise ValueError(f"'{arg}' should be of the form 'address:port'")

      address, port = arg.rsplit(':', 1)

      if not stem.util.connection.is_valid_ipv4_address(address):
        raise ValueError(f"'{address}' isn't a valid IPv4 address")
      elif not stem.util.connection.is_valid_port(port):
        raise ValueError(f"'{port}' isn't a valid port number")

      endpoint_class = stem.ORPort if opt == '--orport' else stem.DirPort
      args['download_from'] = endpoint_class(address, port)
    elif opt in ('-h', '--help'):
      args['print_help'] = True

  # translates our args dict into a named tuple

  Args = collections.namedtuple('Args', args.keys())
  return Args(**args)


def main(argv):
  try:
    args = parse(argv)
  except ValueError as exc:
    print(exc)
    sys.exit(1)

  if args.print_help:
    print(HELP_TEXT)
    sys.exit()

  print('Downloading %s descriptor from %s:%s...\n' % (args.descriptor_type, args.download_from.address, args.download_from.port))
  desc = None

  if args.descriptor_type in ('server', 'extrainfo'):
    if args.descriptor_type == 'server':
      download_func = stem.descriptor.remote.get_server_descriptors
    else:
      download_func = stem.descriptor.remote.get_extrainfo_descriptors

    desc = download_func(
      fingerprints = [args.fingerprint],
      endpoints = [args.download_from],
    ).run()[0]
  elif args.descriptor_type == 'consensus':
    for consensus_desc in stem.descriptor.remote.get_consensus(endpoints = [args.download_from]):
      if consensus_desc.fingerprint == args.fingerprint:
        desc = consensus_desc
        break

    if not desc:
      print(f'Unable to find a descriptor for {args.fingerprint} in the consensus')
      sys.exit(1)
  else:
    print(
        f"'{args.descriptor_type}' is not a recognized descriptor type, options are: {', '.join(VALID_TYPES)}"
    )
    sys.exit(1)

  if desc:
    print(desc)


if __name__ == '__main__':
  main(sys.argv[1:])
