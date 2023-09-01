import collections

import stem.descriptor
import stem.descriptor.remote
import stem.directory

# Query all authority votes asynchronously.

downloader = stem.descriptor.remote.DescriptorDownloader(
  document_handler = stem.descriptor.DocumentHandler.DOCUMENT,
)

# An ordered dictionary ensures queries are finished in the order they were
# added.

queries = collections.OrderedDict()

for name, authority in stem.directory.Authority.from_cache().items():
  if authority.v3ident is None:
    continue  # authority doesn't vote if it lacks a v3ident

  queries[name] = downloader.get_vote(authority)

# Wait for the votes to finish being downloaded, this produces a dictionary of
# authority nicknames to their vote.

votes = {name: query.run()[0] for (name, query) in queries.items()}

# Get a superset of all the fingerprints in all the votes.

all_fingerprints = set()

for vote in votes.values():
  all_fingerprints.update(vote.routers.keys())

# Finally, compare moria1's votes to maatuska's votes.

for fingerprint in sorted(all_fingerprints):
  moria1_vote = votes['moria1'].routers.get(fingerprint)
  maatuska_vote = votes['maatuska'].routers.get(fingerprint)

  if not moria1_vote and not maatuska_vote:
    print(f"both moria1 and maatuska haven't voted about {fingerprint}")
  elif not moria1_vote:
    print(f"moria1 hasn't voted about {fingerprint}")
  elif not maatuska_vote:
    print(f"maatuska hasn't voted about {fingerprint}")
  elif 'Running' in moria1_vote.flags and 'Running' not in maatuska_vote.flags:
    print(f"moria1 has the Running flag but maatuska doesn't: {fingerprint}")
  elif 'Running' in maatuska_vote.flags and 'Running' not in moria1_vote.flags:
    print(f"maatuska has the Running flag but moria1 doesn't: {fingerprint}")
