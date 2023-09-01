import stem.descriptor.remote

bandwidth_file = stem.descriptor.remote.get_bandwidth_file().run()[0]

for fingerprint, measurement in bandwidth_file.measurements.items():
  print(f'Relay {fingerprint}')

  for attr, value in measurement.items():
    print(f'  {attr} = {value}')

  print('')
