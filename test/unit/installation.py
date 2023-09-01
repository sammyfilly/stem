import os
import unittest

import test


class TestInstallation(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    setup_path = os.path.join(test.STEM_BASE, 'setup.py')
    cls.skip_reason = None
    cls.setup_contents = False

    if os.path.exists(setup_path):
      with open(setup_path) as setup_file:
        cls.setup_contents = setup_file.read()
    else:
      cls.skip_reason = '(only for git checkout)'

  def test_installs_all_data_files(self):
    if self.skip_reason:
      self.skipTest(self.skip_reason)

    # Checking that we have all non-source files. Data looks like...
    #
    #   package_data = {
    #     'stem': ['cached_fallbacks.cfg', 'cached_manual.cfg', 'settings.cfg'],
    #   },

    package_data = {}

    for line in self.setup_contents.split('package_data = {\n', 1)[1].splitlines():
      if '},' in line:
        break

      directory = line.strip().split()[0][1:-2]
      files = line.strip().split(' ', 1)[1][2:-3].split("', '")
      package_data[directory] = files

    data_files = []

    for module, files in package_data.items():
      data_files.extend(
          os.path.join(test.STEM_BASE, module.replace('.', os.path.sep),
                       module_file) for module_file in files)
    for path in data_files:
      if not os.path.exists(path):
        self.fail(f"setup.py installs a data file that doesn't exist: {path}")

    for entry in os.walk(os.path.join(test.STEM_BASE, 'stem')):
      directory = entry[0]

      if directory.endswith('__pycache__'):
        continue

      for filename in entry[2]:
        path = os.path.join(directory, filename)
        file_type = path.split('.')[-1]

        if file_type in (['py'] + test.IGNORED_FILE_TYPES):
          continue
        elif path not in data_files:
          self.fail(f"setup.py doesn't install {path}")
