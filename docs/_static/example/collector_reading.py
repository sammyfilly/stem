import datetime
import stem.descriptor.collector

yesterday = datetime.datetime.utcnow() - datetime.timedelta(days = 1)

exits = {
    desc.fingerprint: desc
    for desc in stem.descriptor.collector.get_server_descriptors(
        start=yesterday) if desc.exit_policy.is_exiting_allowed()
}
print('%i relays published an exiting policy today...\n' % len(exits))

for fingerprint, desc in exits.items():
  print(f'  {desc.nickname} ({fingerprint})')
