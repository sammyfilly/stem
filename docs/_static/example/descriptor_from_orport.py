import stem.descriptor.remote

# Unlike the above example, this one downloads specifically through the
# ORPort of moria1 (long time tor directory authority).

try:
  consensus = stem.descriptor.remote.get_consensus(
    endpoints = (stem.ORPort('128.31.0.34', 9101),)
  )

  for desc in consensus:
    print(f'found relay {desc.nickname} ({desc.fingerprint})')
except Exception as exc:
  print(f'Unable to retrieve the consensus: {exc}')
