import stem.descriptor.remote
import stem.directory

downloader = stem.descriptor.remote.DescriptorDownloader()

queries = {
    authority.nickname: downloader.query(
        '/tor/status-vote/current/authority',
        endpoints=[(authority.address, authority.dir_port)],
    )
    for authority in stem.directory.Authority.from_cache().values()
}
for authority_name, query in queries.items():
  try:
    print(f"Getting {authority_name}'s vote from {query.download_url}:")

    measured, unmeasured = 0, 0

    for desc in query.run():
      if desc.measured:
        measured += 1
      else:
        unmeasured += 1

    if measured == 0:
      print(f'  {authority_name} is not a bandwidth authority')
    else:
      print('  %i measured entries and %i unmeasured' % (measured, unmeasured))
  except Exception as exc:
    print(f'  failed to get the vote ({exc})')
