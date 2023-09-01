import sys

import stem.descriptor.remote
import stem.util.tor_tools


def download_descriptors(fingerprint):
  """
  Downloads the descriptors we need to validate this relay. Downloads are
  parallelized, providing the caller with a tuple of the form...

    (router_status_entry, server_descriptor, extrainfo_descriptor)
  """

  conensus_query = stem.descriptor.remote.get_consensus()
  server_desc_query = stem.descriptor.remote.get_server_descriptors(fingerprint)
  extrainfo_query = stem.descriptor.remote.get_extrainfo_descriptors(fingerprint)

  router_status_entries = list(filter(lambda desc: desc.fingerprint == fingerprint, conensus_query.run()))

  if len(router_status_entries) != 1:
    raise OSError(f"Unable to find relay '{fingerprint}' in the consensus")

  return (
    router_status_entries[0],
    server_desc_query.run()[0],
    extrainfo_query.run()[0],
  )


def validate_relay(fingerprint):
  print('')  # blank line

  if not stem.util.tor_tools.is_valid_fingerprint(fingerprint):
    print(f"'{fingerprint}' is not a valid relay fingerprint")
    sys.exit(1)

  try:
    router_status_entry, server_desc, extrainfo_desc = download_descriptors(fingerprint)
  except Exception as exc:
    print(exc)
    sys.exit(1)

  if router_status_entry.digest == server_desc.digest():
    print('Server descriptor digest is correct')
  else:
    print(
        f'Server descriptor digest invalid, expected {router_status_entry.digest} but is {server_desc.digest()}'
    )

  if server_desc.extra_info_digest == extrainfo_desc.digest():
    print('Extrainfo descriptor digest is correct')
  else:
    print(
        f'Extrainfo descriptor digest invalid, expected {server_desc.extra_info_digest} but is {extrainfo_desc.digest()}'
    )


if __name__ == '__main__':
  fingerprint = input('What relay fingerprint would you like to validate?\n')
  validate_relay(fingerprint)
