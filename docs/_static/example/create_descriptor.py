from stem.descriptor.server_descriptor import RelayDescriptor

# prints 'caerSidi (71.35.133.197:9001)'
desc = RelayDescriptor.create()
print(f'{desc.nickname} ({desc.address}:{desc.or_port})')

# prints 'demo (127.0.0.1:80)'
desc = RelayDescriptor.create({'router': 'demo 127.0.0.1 80 0 0'})
print(f'{desc.nickname} ({desc.address}:{desc.or_port})')
