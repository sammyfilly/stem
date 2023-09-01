#!/usr/bin/env python
# Copyright 2015-2020, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Caches tor's latest manual content. Run this to pick new man page changes.
"""


import re
import sys
import urllib.request

import stem.manual
import stem.util.system

GITWEB_MAN_LOG = 'https://gitweb.torproject.org/tor.git/log/doc/tor.1.txt'
MAN_LOG_LINK = b"href='/tor.git/commit/doc/tor.1.txt\\?id=([^']*)'"

if __name__ == '__main__':
  try:
    man_log_page = urllib.request.urlopen(GITWEB_MAN_LOG).read()
    man_commit = re.search(MAN_LOG_LINK, man_log_page).group(1).decode('utf-8')
  except:
    print(
        f"Unable to determine the latest commit to edit tor's man page: {sys.exc_info()[1]}"
    )
    sys.exit(1)

  try:
    stem_commit = stem.util.system.call('git rev-parse HEAD')[0]
  except OSError as exc:
    print(f"Unable to determine stem's current commit: {exc}")
    sys.exit(1)

  print(f'Latest tor commit editing man page: {man_commit}')
  print(f'Current stem commit: {stem_commit}')
  print('')

  try:
    cached_manual = stem.manual.Manual.from_cache()
    db_schema = cached_manual.schema
  except stem.manual.SchemaMismatch as exc:
    cached_manual, db_schema = None, exc.database_schema
  except OSError:
    cached_manual, db_schema = None, None  # local copy has been deleted

  if db_schema != stem.manual.SCHEMA_VERSION:
    print(
        f'Cached database schema is out of date (was {db_schema}, but current version is {stem.manual.SCHEMA_VERSION})'
    )
    cached_manual = None

  latest_manual = stem.manual.Manual.from_remote()

  if cached_manual:
    if cached_manual == latest_manual:
      print('Manual information is already up to date, nothing to do.')
      sys.exit(0)

    print('Differences detected...\n')
    print(stem.manual._manual_differences(cached_manual, latest_manual))

  latest_manual.man_commit = man_commit
  latest_manual.stem_commit = stem_commit
  latest_manual.save(stem.manual.CACHE_PATH)
