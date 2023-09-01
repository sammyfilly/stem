# Copyright 2015-2020, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Information available about Tor from `its manual
<https://www.torproject.org/docs/tor-manual.html.en>`_. This provides three
methods of getting this information...

* :func:`~stem.manual.Manual.from_cache` provides manual content bundled with
  Stem. This is the fastest and most reliable method but only as up-to-date as
  Stem's release.

* :func:`~stem.manual.Manual.from_man` reads Tor's local man page for
  information about it.

* :func:`~stem.manual.Manual.from_remote` fetches the latest manual information
  remotely. This is the slowest and least reliable method but provides the most
  recent information about Tor.

Manual information includes arguments, signals, and probably most usefully the
torrc configuration options. For example, say we want a little script that told
us what our torrc options do...

.. literalinclude:: /_static/example/manual_config_options.py
   :language: python

|

.. image:: /_static/manual_output.png

|

**Module Overview:**

::

  query - performs a query on our cached sqlite manual information
  is_important - Indicates if a configuration option is of particularly common importance.
  download_man_page - Downloads tor's latest man page.

  Manual - Information about Tor available from its manual.
   | |- from_cache - Provides manual information cached with Stem.
   | |- from_man - Retrieves manual information from its man page.
   | +- from_remote - Retrieves manual information remotely from tor's latest manual.
   |
   +- save - writes the manual contents to a given location

.. versionadded:: 1.5.0
"""

import collections
import functools
import os
import shutil
import sys
import tempfile
import urllib.request

import stem
import stem.util
import stem.util.conf
import stem.util.enum
import stem.util.log
import stem.util.str_tools
import stem.util.system

from typing import Any, BinaryIO, Dict, List, Mapping, Optional, Sequence, Tuple, Union

Category = stem.util.enum.Enum('GENERAL', 'CLIENT', 'CIRCUIT_TIMEOUT', 'DORMANT_MODE', 'NODE_SELECTION', 'RELAY', 'STATISTIC', 'DIRECTORY', 'AUTHORITY', 'HIDDEN_SERVICE', 'DENIAL_OF_SERVICE', 'TESTING', 'UNKNOWN')
GITWEB_MANUAL_URL = 'https://gitweb.torproject.org/tor.git/plain/doc/man/tor.1.txt'
CACHE_PATH = os.path.join(os.path.dirname(__file__), 'cached_manual.sqlite')
DATABASE = None  # cache database connections
HAS_ENCODING_ARG = not stem.util.system.is_mac() and not stem.util.system.is_bsd() and not stem.util.system.is_slackware()

SCHEMA_VERSION = 1  # version of our scheme, bump this if you change the following
SCHEMA = (
  'CREATE TABLE schema(version INTEGER)',
  'INSERT INTO schema(version) VALUES (%i)' % SCHEMA_VERSION,

  'CREATE TABLE metadata(name TEXT, synopsis TEXT, description TEXT, man_commit TEXT, stem_commit TEXT)',
  'CREATE TABLE commandline(name TEXT PRIMARY KEY, description TEXT)',
  'CREATE TABLE signals(name TEXT PRIMARY KEY, description TEXT)',
  'CREATE TABLE files(name TEXT PRIMARY KEY, description TEXT)',
  'CREATE TABLE torrc(key TEXT PRIMARY KEY, name TEXT, category TEXT, usage TEXT, summary TEXT, description TEXT, position INTEGER)',
)

CATEGORY_SECTIONS = collections.OrderedDict((
  ('GENERAL OPTIONS', Category.GENERAL),
  ('CLIENT OPTIONS', Category.CLIENT),
  ('CIRCUIT TIMEOUT OPTIONS', Category.CIRCUIT_TIMEOUT),
  ('DORMANT MODE OPTIONS', Category.DORMANT_MODE),
  ('NODE SELECTION OPTIONS', Category.NODE_SELECTION),
  ('SERVER OPTIONS', Category.RELAY),
  ('STATISTICS OPTIONS', Category.STATISTIC),
  ('DIRECTORY SERVER OPTIONS', Category.DIRECTORY),
  ('DIRECTORY AUTHORITY SERVER OPTIONS', Category.AUTHORITY),
  ('HIDDEN SERVICE OPTIONS', Category.HIDDEN_SERVICE),
  ('DENIAL OF SERVICE MITIGATION OPTIONS', Category.DENIAL_OF_SERVICE),
  ('TESTING NETWORK OPTIONS', Category.TESTING),
))


class SchemaMismatch(OSError):
  """
  Database schema doesn't match what Stem supports.

  .. versionadded:: 1.6.0

  :var int database_schema: schema of the database
  :var tuple supported_schemas: schemas library supports
  """

  def __init__(self, message: str, database_schema: int, supported_schemas: Tuple[int]) -> None:
    super(SchemaMismatch, self).__init__(message)
    self.database_schema = database_schema
    self.supported_schemas = supported_schemas


def query(query: str, *param: str) -> 'sqlite3.Cursor':  # type: ignore
  """
  Performs the given query on our sqlite manual cache. This database should
  be treated as being read-only. File permissions generally enforce this, and
  in the future will be enforced by this function as well.

  ::

    >>> import stem.manual
    >>> print(stem.manual.query('SELECT description FROM torrc WHERE key=?', 'CONTROLSOCKET').fetchone()[0])
    Like ControlPort, but listens on a Unix domain socket, rather than a TCP socket.  0 disables ControlSocket. (Unix and Unix-like systems only.) (Default: 0)

  .. versionadded:: 1.6.0

  :param query: query to run on the cache
  :param param: query parameters

  :returns: :class:`sqlite3.Cursor` with the query results

  :raises:
    * **ImportError** if the sqlite3 module is unavailable
    * **sqlite3.OperationalError** if query fails
  """

  try:
    import sqlite3
  except ImportError:
    raise ImportError('Querying requires the sqlite3 module')

  # The only reason to explicitly close the sqlite connection is to ensure
  # transactions are committed. Since we're only using read-only access this
  # doesn't matter, and can allow interpreter shutdown to do the needful.

  global DATABASE

  if DATABASE is None:
    DATABASE = sqlite3.connect(f'file:{CACHE_PATH}?mode=ro', uri=True)

  return DATABASE.execute(query, param)


class ConfigOption(object):
  """
  Tor configuration attribute found in its torrc.

  :var str name: name of the configuration option
  :var stem.manual.Category category: category the config option was listed
    under, this is Category.UNKNOWN if we didn't recognize the category
  :var str usage: arguments accepted by the option
  :var str summary: brief description of what the option does
  :var str description: longer manual description with details
  """

  def __init__(self, name: str, category: 'stem.manual.Category' = Category.UNKNOWN, usage: str = '', summary: str = '', description: str = '') -> None:
    self.name = name
    self.category = category
    self.usage = usage
    self.summary = summary
    self.description = description

  def __hash__(self) -> int:
    return stem.util._hash_attr(self, 'name', 'category', 'usage', 'summary', 'description', cache = True)

  def __eq__(self, other: Any) -> bool:
    return hash(self) == hash(other) if isinstance(other, ConfigOption) else False

  def __ne__(self, other: Any) -> bool:
    return not self == other


@functools.lru_cache()
def _config(lowercase: bool = True) -> Dict[str, Union[List[str], str]]:
  """
  Provides a dictionary for our settings.cfg. This has a couple categories...

    * manual.important (list) - configuration options considered to be important
    * manual.summary.* (str) - summary descriptions of config options

  :param lowercase: uses lowercase keys if **True** to allow for case
    insensitive lookups
  """

  config = stem.util.conf.Config()
  config_path = os.path.join(os.path.dirname(__file__), 'settings.cfg')

  try:
    config.load(config_path)
    config_dict = dict([(key.lower() if lowercase else key, config.get_value(key)) for key in config.keys() if key.startswith('manual.summary.')])
    config_dict['manual.important'] = [name.lower() if lowercase else name for name in config.get_value('manual.important', [], multiple = True)]
    return config_dict
  except Exception as exc:
    stem.util.log.warn(
        f"BUG: stem failed to load its internal manual information from '{config_path}': {exc}"
    )
    return {}


def _manual_differences(previous_manual: 'stem.manual.Manual', new_manual: 'stem.manual.Manual') -> str:
  """
  Provides a description of how two manuals differ.
  """

  lines = []

  for attr in ('name', 'synopsis', 'description', 'commandline_options', 'signals', 'files', 'config_options'):
    previous_attr = getattr(previous_manual, attr)
    new_attr = getattr(new_manual, attr)

    if previous_attr != new_attr:
      lines.append("* Manual's %s attribute changed\n" % attr)

      if attr in ('name', 'synopsis', 'description'):
        lines.extend((
            '  Previously...\n\n%s\n' % previous_attr,
            '  Updating to...\n\n%s' % new_attr,
        ))
      elif attr == 'config_options':
        for config_name, config_attr in new_attr.items():
          previous = previous_attr.get(config_name)

          if previous is None:
            lines.append(f'  adding new config option => {config_name}')
          elif config_attr != previous:
            lines.extend(
                f'  modified {config_name} ({attr}) => {getattr(config_attr, attr)}'
                for attr in (
                    'name',
                    'category',
                    'usage',
                    'summary',
                    'description',
                ) if getattr(config_attr, attr) != getattr(previous, attr))
        lines.extend(f'  removing config option => {config_name}'
                     for config_name in set(previous_attr.keys()).difference(
                         new_attr.keys()))
      else:
        added_items = set(new_attr.items()).difference(previous_attr.items())
        removed_items = set(previous_attr.items()).difference(new_attr.items())

        lines.extend('  adding %s => %s' % added_item for added_item in added_items)
        lines.extend('  removing %s => %s' % removed_item
                     for removed_item in removed_items)
      lines.append('\n')

  return '\n'.join(lines)


def is_important(option: str) -> bool:
  """
  Indicates if a configuration option of particularly common importance or not.

  :param option: tor configuration option to check

  :returns: **bool** that's **True** if this is an important option and
    **False** otherwise
  """

  return option.lower() in _config()['manual.important']


def download_man_page(path: Optional[str] = None, file_handle: Optional[BinaryIO] = None, url: str = GITWEB_MANUAL_URL, timeout: int = 20) -> None:
  """
  Downloads tor's latest man page from `gitweb.torproject.org
  <https://gitweb.torproject.org/tor.git/plain/doc/tor.1.txt>`_. This method is
  both slow and unreliable - please see the warnings on
  :func:`~stem.manual.Manual.from_remote`.

  :param path: path to save tor's man page to
  :param file_handle: file handler to save tor's man page to
  :param url: url to download tor's asciidoc manual from
  :param timeout: seconds to wait before timing out the request

  :raises: **OSError** if unable to retrieve the manual
  """

  if not path and not file_handle:
    raise ValueError("Either the path or file_handle we're saving to must be provided")
  elif not stem.util.system.is_available('a2x'):
    raise OSError('We require a2x from asciidoc to provide a man page')

  with tempfile.TemporaryDirectory() as dirpath:
    asciidoc_path = os.path.join(dirpath, 'tor.1.txt')
    manual_path = os.path.join(dirpath, 'tor.1')

    try:
      with open(asciidoc_path, 'wb') as asciidoc_file:
        request = urllib.request.urlopen(url, timeout = timeout)
        shutil.copyfileobj(request, asciidoc_file)
    except:
      exc, stacktrace = sys.exc_info()[1:3]
      message = f"Unable to download tor's manual from {url} to {asciidoc_path}: {exc}"
      raise stem.DownloadFailed(url, exc, stacktrace, message)

    try:
      stem.util.system.call(f'a2x -f manpage {asciidoc_path}')

      if not os.path.exists(manual_path):
        raise OSError('no man page was generated')
    except stem.util.system.CallError as exc:
      raise OSError(
          f"Unable to run '{exc.command}': {stem.util.str_tools._to_unicode(exc.stderr)}"
      )

    if path:
      try:
        path_dir = os.path.dirname(path)

        if not os.path.exists(path_dir):
          os.makedirs(path_dir)

        shutil.copyfile(manual_path, path)
      except OSError as exc:
        raise OSError(exc)

    if file_handle:
      with open(manual_path, 'rb') as manual_file:
        shutil.copyfileobj(manual_file, file_handle)
        file_handle.flush()


class Manual(object):
  """
  Parsed tor man page. Tor makes no guarantees about its man page format so
  this may not always be compatible. If not you can use the cached manual
  information stored with Stem.

  This does not include every bit of information from the tor manual. For
  instance, I've excluded the 'THE CONFIGURATION FILE FORMAT' section. If
  there's a part you'd find useful then file an issue and we can add it.

  :var str name: brief description of the tor command
  :var str synopsis: brief tor command usage
  :var str description: general description of what tor does

  :var collections.OrderedDict commandline_options: mapping of commandline arguments to their descripton
  :var collections.OrderedDict signals: mapping of signals tor accepts to their description
  :var collections.OrderedDict files: mapping of file paths to their description

  :var collections.OrderedDict config_options: :class:`~stem.manual.ConfigOption` tuples for tor configuration options

  :var str man_commit: latest tor commit editing the man page when this
    information was cached
  :var str stem_commit: stem commit to cache this manual information
  """

  def __init__(self, name: str, synopsis: str, description: str, commandline_options: Mapping[str, str], signals: Mapping[str, str], files: Mapping[str, str], config_options: Mapping[str, 'stem.manual.ConfigOption']) -> None:
    self.name = name
    self.synopsis = synopsis
    self.description = description
    self.commandline_options = collections.OrderedDict(commandline_options)
    self.signals = collections.OrderedDict(signals)
    self.files = collections.OrderedDict(files)
    self.config_options = collections.OrderedDict(config_options)
    self.man_commit = None
    self.stem_commit = None
    self.schema = None

  @staticmethod
  def from_cache(path: Optional[str] = None) -> 'stem.manual.Manual':
    """
    Provides manual information cached with Stem. Unlike
    :func:`~stem.manual.Manual.from_man` and
    :func:`~stem.manual.Manual.from_remote` this doesn't have any system
    requirements, and is faster too. Only drawback is that this manual
    content is only as up to date as the Stem release we're using.

    .. versionchanged:: 1.6.0
       Added support for sqlite cache. Support for
       :class:`~stem.util.conf.Config` caches will be dropped in Stem 2.x.

    :param path: cached manual content to read, if not provided this uses
      the bundled manual information

    :returns: :class:`~stem.manual.Manual` with our bundled manual information

    :raises:
      * **ImportError** if cache is sqlite and the sqlite3 module is
        unavailable
      * **OSError** if a **path** was provided and we were unable to read
        it or the schema is out of date
    """

    try:
      import sqlite3
    except ImportError:
      raise ImportError('Reading a sqlite cache requires the sqlite3 module')

    if path is None:
      path = CACHE_PATH

    if not os.path.exists(path):
      raise OSError(f"{path} doesn't exist")

    with sqlite3.connect(path) as conn:
      try:
        schema = conn.execute('SELECT version FROM schema').fetchone()[0]

        if schema != SCHEMA_VERSION:
          raise SchemaMismatch(
              f"Stem's current manual schema version is {SCHEMA_VERSION}, but {path} was version {schema}",
              schema,
              (SCHEMA_VERSION, ),
          )

        name, synopsis, description, man_commit, stem_commit = conn.execute('SELECT name, synopsis, description, man_commit, stem_commit FROM metadata').fetchone()
      except sqlite3.OperationalError as exc:
        raise OSError(f'Failed to read database metadata from {path}: {exc}')

      commandline = dict(conn.execute('SELECT name, description FROM commandline').fetchall())
      signals = dict(conn.execute('SELECT name, description FROM signals').fetchall())
      files = dict(conn.execute('SELECT name, description FROM files').fetchall())

      config_options = collections.OrderedDict()

      for entry in conn.execute('SELECT name, category, usage, summary, description FROM torrc ORDER BY position').fetchall():
        option, category, usage, summary, option_description = entry
        config_options[option] = ConfigOption(option, category, usage, summary, option_description)

      manual = Manual(name, synopsis, description, commandline, signals, files, config_options)
      manual.man_commit = man_commit
      manual.stem_commit = stem_commit
      manual.schema = schema

      return manual

  @staticmethod
  def from_man(man_path: str = 'tor') -> 'stem.manual.Manual':
    """
    Reads and parses a given man page.

    On OSX the man command doesn't have an '--encoding' argument so its results
    may not quite match other platforms. For instance, it normalizes long
    dashes into '--'.

    :param man_path: path argument for 'man', for example you might want
      '/path/to/tor/doc/tor.1' to read from tor's git repository

    :returns: :class:`~stem.manual.Manual` for the system's man page

    :raises: **OSError** if unable to retrieve the manual
    """

    man_cmd = (
        f"man {'--encoding=ascii' if HAS_ENCODING_ARG else ''} -P cat {man_path}"
    )

    try:
      man_output = stem.util.system.call(man_cmd, env = {'MANWIDTH': '10000000'})
    except OSError as exc:
      raise OSError(f"Unable to run '{man_cmd}': {exc}")

    categories = _get_categories(man_output)
    config_options = collections.OrderedDict()  # type: collections.OrderedDict[str, stem.manual.ConfigOption]

    for category_header, category_enum in CATEGORY_SECTIONS.items():
      _add_config_options(config_options, category_enum, categories.get(category_header, []))

    for category in categories:
      if category.endswith(' OPTIONS') and category not in CATEGORY_SECTIONS and category not in ('COMMAND-LINE OPTIONS', 'NON-PERSISTENT OPTIONS'):
        _add_config_options(config_options, Category.UNKNOWN, categories.get(category, []))

    return Manual(
      _join_lines(categories.get('NAME', [])),
      _join_lines(categories.get('SYNOPSIS', [])),
      _join_lines(categories.get('DESCRIPTION', [])),
      _get_indented_descriptions(categories.get('COMMAND-LINE OPTIONS', [])),
      _get_indented_descriptions(categories.get('SIGNALS', [])),
      _get_indented_descriptions(categories.get('FILES', [])),
      config_options,
    )

  @staticmethod
  def from_remote(timeout: int = 60) -> 'stem.manual.Manual':
    """
    Reads and parses the latest tor man page `from gitweb.torproject.org
    <https://gitweb.torproject.org/tor.git/plain/doc/tor.1.txt>`_. Note that
    while convenient, this reliance on GitWeb means you should alway call with
    a fallback, such as...

    ::

      try:
        manual = stem.manual.from_remote()
      except OSError:
        manual = stem.manual.from_cache()

    In addition to our GitWeb dependency this requires 'a2x' which is part of
    `asciidoc <http://asciidoc.org/INSTALL.html>`_ and... isn't quick.
    Personally this takes ~7.41s, breaking down for me as follows...

      * 1.67s to download tor.1.txt
      * 5.57s to convert the asciidoc to a man page
      * 0.17s for stem to read and parse the manual

    :param timeout: seconds to wait before timing out the request

    :returns: latest :class:`~stem.manual.Manual` available for tor

    :raises: **OSError** if unable to retrieve the manual
    """

    with tempfile.NamedTemporaryFile() as tmp:
      download_man_page(file_handle = tmp, timeout = timeout)  # type: ignore
      return Manual.from_man(tmp.name)

  def save(self, path: str) -> None:
    """
    Persists the manual content to a given location.

    .. versionchanged:: 1.6.0
       Added support for sqlite cache. Support for
       :class:`~stem.util.conf.Config` caches will be dropped in Stem 2.x.

    :param path: path to save our manual content to

    :raises:
      * **ImportError** if saving as sqlite and the sqlite3 module is
        unavailable
      * **OSError** if unsuccessful
    """

    try:
      import sqlite3
    except ImportError:
      raise ImportError('Saving a sqlite cache requires the sqlite3 module')

    tmp_path = f'{path}.new'

    if os.path.exists(tmp_path):
      os.remove(tmp_path)

    with sqlite3.connect(tmp_path) as conn:
      for cmd in SCHEMA:
        conn.execute(cmd)

      conn.execute('INSERT INTO metadata(name, synopsis, description, man_commit, stem_commit) VALUES (?,?,?,?,?)', (self.name, self.synopsis, self.description, self.man_commit, self.stem_commit))

      for k, v in self.commandline_options.items():
        conn.execute('INSERT INTO commandline(name, description) VALUES (?,?)', (k, v))

      for k, v in self.signals.items():
        conn.execute('INSERT INTO signals(name, description) VALUES (?,?)', (k, v))

      for k, v in self.files.items():
        conn.execute('INSERT INTO files(name, description) VALUES (?,?)', (k, v))

      for i, v in enumerate(self.config_options.values()):
        conn.execute('INSERT INTO torrc(key, name, category, usage, summary, description, position) VALUES (?,?,?,?,?,?,?)', (v.name.upper(), v.name, v.category, v.usage, v.summary, v.description, i))

    if os.path.exists(path):
      os.remove(path)

    os.rename(tmp_path, path)

  def __hash__(self) -> int:
    return stem.util._hash_attr(self, 'name', 'synopsis', 'description', 'commandline_options', 'signals', 'files', 'config_options', cache = True)

  def __eq__(self, other: Any) -> bool:
    return hash(self) == hash(other) if isinstance(other, Manual) else False

  def __ne__(self, other: Any) -> bool:
    return not self == other


def _get_categories(content: Sequence[str]) -> Dict[str, List[str]]:
  """
  The man page is headers followed by an indented section. First pass gets
  the mapping of category titles to their lines.
  """

  # skip header and footer lines

  if content and 'TOR(1)' in content[0]:
    content = content[1:]

  if content and content[-1].startswith('Tor'):
    content = content[:-1]

  categories = collections.OrderedDict()
  category = None
  lines = []  # type: List[str]

  for line in content:
    # replace non-ascii characters
    #
    #   \u2019 - smart single quote
    #   \u2014 - extra long dash
    #   \xb7 - centered dot

    line = line.replace(chr(0x2019), "'").replace(chr(0x2014), '-').replace(chr(0xb7), '*')

    if line and not line.startswith(' '):
      if category:
        if lines and lines[-1] == '':
          lines = lines[:-1]  # sections end with an extra empty line

        categories[category] = lines

      category, lines = line.strip(), []
    else:
      if line.startswith('       '):
        line = line[7:]  # contents of a section have a seven space indentation

      lines.append(line)

  if category:
    categories[category] = lines

  return categories


def _get_indented_descriptions(lines: Sequence[str]) -> Dict[str, str]:
  """
  Parses the commandline argument and signal sections. These are options
  followed by an indented description. For example...

  ::

    -f FILE
        Specify a new configuration file to contain further Tor configuration
        options OR pass - to make Tor read its configuration from standard
        input. (Default: /usr/local/etc/tor/torrc, or $HOME/.torrc if that file
        is not found)

  There can be additional paragraphs not related to any particular argument but
  ignoring those.
  """

  options = collections.OrderedDict()  # type: collections.OrderedDict[str, List[str]]
  last_arg = None

  for line in lines:
    if line == '    Note':
      last_arg = None  # manual has several indented 'Note' blocks
    elif line and not line.startswith(' '):
      options[line], last_arg = [], line
    elif last_arg and line.startswith('    '):
      options[last_arg].append(line[4:])

  return dict([(arg, ' '.join(desc_lines)) for arg, desc_lines in options.items() if desc_lines])


def _add_config_options(config_options: Dict[str, 'stem.manual.ConfigOption'], category: str, lines: Sequence[str]) -> None:
  """
  Parses a section of tor configuration options. These have usage information,
  followed by an indented description. For instance...

  ::

    ConnLimit NUM
        The minimum number of file descriptors that must be available to the
        Tor process before it will start. Tor will ask the OS for as many file
        descriptors as the OS will allow (you can find this by "ulimit -H -n").
        If this number is less than ConnLimit, then Tor will refuse to start.


        You probably don't need to adjust this. It has no effect on Windows
        since that platform lacks getrlimit(). (Default: 1000)
  """

  def add_option(title: str, description: List[str]) -> None:
    if 'PER INSTANCE OPTIONS' in title:
      return  # skip, unfortunately amid the options

    if ', ' in title:
      # Line actually had multiple options with the same description. For
      # example...
      #
      #   AlternateBridgeAuthority [nickname], AlternateDirAuthority [nickname]

      for subtitle in title.split(', '):
        add_option(subtitle, description)
    else:
      name, usage = title.split(' ', 1) if ' ' in title else (title, '')
      summary = str(_config().get(f'manual.summary.{name.lower()}', ''))
      config_options[name] = ConfigOption(name, category, usage, summary, _join_lines(description).strip())

  # Remove the section's description by finding the sentence the section
  # ends with.

  end_indices = [i for (i, line) in enumerate(lines) if ('The following options' in line or 'PER SERVICE OPTIONS' in line)]

  if end_indices:
    lines = lines[max(end_indices):]  # trim to the description paragrah
    lines = lines[lines.index(''):]  # drop the paragraph

  last_title = None
  description = []  # type: List[str]

  for line in lines:
    if line and not line.startswith(' '):
      if last_title:
        add_option(last_title, description)

      last_title, description = line, []
    else:
      if line.startswith('    '):
        line = line[4:]

      description.append(line)

  if last_title:
    add_option(last_title, description)


def _join_lines(lines: Sequence[str]) -> str:
  """
  Simple join, except we want empty lines to still provide a newline.
  """

  result = []  # type: List[str]

  for line in lines:
    if line:
      result.append(line + '\n')

    elif result and result[-1] != '\n':
      result.append('\n')
  return ''.join(result).strip()
