from stem.control import Controller

with Controller.from_port(port = 9051) as controller:
  controller.authenticate()  # provide the password here if you set one

  bytes_read = controller.get_info('traffic/read')
  bytes_written = controller.get_info('traffic/written')

  print(f'My Tor relay has read {bytes_read} bytes and written {bytes_written}.')
