from stem.descriptor import parse_file

for desc in parse_file('/home/atagar/.tor/cached-consensus'):
  print(f'found relay {desc.nickname} ({desc.fingerprint})')
