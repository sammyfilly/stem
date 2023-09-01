#!/usr/bin/env python
# Copyright 2011-2020, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Runs unit and integration tests. For usage information run this with '--help'.
"""

import errno
import io
import importlib
import logging
import logging.handlers
import multiprocessing
import os
import queue
import signal
import sys
import threading
import time
import traceback
import unittest

import stem.util.conf
import stem.util.log
import stem.util.system
import stem.util.test_tools
import stem.version

import test
import test.arguments
import test.output
import test.runner
import test.task

from test.output import STATUS, SUCCESS, ERROR, NO_NL, STDERR, println

CONFIG = stem.util.conf.config_dict('test', {
  'integ.test_directory': './test/data',
  'test.unit_tests': '',
  'test.integ_tests': '',
})

MOCK_UNAVAILABLE_MSG = """\
To run stem's tests you'll need mock...

https://pypi.org/project/mock/
"""

MOCK_OUT_OF_DATE_MSG = """\
To run stem's tests you'll need mock. You have version %s, but you need
version 0.8.0 or later...

https://pypi.org/project/mock/
"""

NEW_CAPABILITIES_FOUND = """\
Your version of Tor has capabilities stem currently isn't taking advantage of.
If you're running the latest version of stem then please file a ticket on:

  https://github.com/torproject/stem/issues/

New capabilities are:
"""


def log_traceback(sig, frame):
  """
  Dump the stacktraces of all threads on stderr.
  """

  signal_name = next(
      (attr_name for attr_name, value in signal.__dict__.items()
       if attr_name.startswith('SIG') and value == sig),
      str(sig),
  )
  lines = [
    '',  # initial NL so we start on our own line
    '=' * 80,
    'Signal %s received by thread %s in process %i' % (signal_name, threading.current_thread().name, os.getpid()),
  ]

  for thread_name, stacktrace in test.output.thread_stacktraces().items():
    lines.extend(('-' * 80, f'{thread_name} thread stacktrace', '', stacktrace))
  lines.append('=' * 80)
  println('\n'.join(lines), STDERR)

  # propagate the signal to any multiprocessing children

  for p in multiprocessing.active_children():
    try:
      os.kill(p.pid, sig)
    except OSError as exc:
      if exc.errno != errno.ESRCH:
        raise exc

  if sig == signal.SIGABRT:
    # we need to use os._exit() to abort every thread in the interpreter,
    # rather than raise a SystemExit exception that can be caught
    os._exit(-1)


def get_unit_tests(module_prefixes, exclude):
  """
  Provides the classes for our unit tests.

  :param list module_prefixes: only provide the test if the module starts with
    any of these substrings
  :param list exclude: test modules explicitly excluded

  :returns: an **iterator** for our unit tests
  """

  return _get_tests(CONFIG['test.unit_tests'].splitlines(), module_prefixes, exclude)


def get_integ_tests(module_prefixes, exclude):
  """
  Provides the classes for our integration tests.

  :param list module_prefixes: only provide the test if the module starts with
    any of these substrings
  :param list exclude: test modules explicitly excluded

  :returns: an **iterator** for our integration tests
  """

  return _get_tests(CONFIG['test.integ_tests'].splitlines(), module_prefixes, exclude)


def _get_tests(modules, module_prefixes, exclude):
  for import_name in modules:
    cropped_name = test.arguments.crop_module_name(import_name)
    cropped_name = cropped_name.rsplit('.', 1)[0]  # exclude the class name

    if exclude:
      skip = any(
          cropped_name.startswith(exclude_prefix)
          for exclude_prefix in exclude)
      if skip:
        continue

    if not module_prefixes:
      yield import_name
    else:
      for prefix in module_prefixes:
        if cropped_name.startswith(prefix):
          yield import_name
          break
        elif prefix.startswith(cropped_name):
          # single test for this module

          test_name = prefix.rsplit('.', 1)[1]
          yield f'{import_name}.{test_name}'
          break


def main():
  start_time = time.time()

  major_version, minor_version = sys.version_info[:2]

  if major_version < 3 or (major_version == 3 and minor_version < 6):
    println('stem requires python version 3.6 or greater\n')
    sys.exit(1)

  signal.signal(signal.SIGABRT, log_traceback)
  signal.signal(signal.SIGUSR1, log_traceback)

  test_config = stem.util.conf.get_config('test')
  test_config.load(os.path.join(test.STEM_BASE, 'test', 'settings.cfg'))

  if 'STEM_TEST_CONFIG' in os.environ:
    test_config.load(os.environ['STEM_TEST_CONFIG'])

  try:
    args = test.arguments.Arguments.parse(sys.argv[1:])
    test.task.TOR_VERSION.args = (args.tor_path,)
    test.output.SUPPRESS_STDOUT = args.quiet
  except ValueError as exc:
    println(str(exc))
    sys.exit(1)

  if args.print_help:
    println(test.arguments.Arguments.get_help())
    sys.exit()
  elif not args.run_unit and not args.run_integ:
    println('Nothing to run (for usage provide --help)\n')
    sys.exit()

  test.task.run(
    'INITIALISING',
    test.task.STEM_VERSION,
    test.task.TOR_VERSION if args.run_integ else None,
    test.task.PYTHON_VERSION,
    test.task.PLATFORM_VERSION,
    test.task.CRYPTO_VERSION,
    test.task.PYFLAKES_VERSION,
    test.task.PYCODESTYLE_VERSION,
    test.task.MYPY_VERSION,
    test.task.CLEAN_PYC,
    test.task.UNUSED_TESTS,
    test.task.IMPORT_TESTS,
    test.task.REMOVE_TOR_DATA_DIR if args.run_integ else None,
    test.task.PYFLAKES_TASK if not args.specific_test else None,
    test.task.PYCODESTYLE_TASK if not args.specific_test else None,
    test.task.MYPY_TASK if not args.specific_test else None,
  )

  # Test logging. If '--log-file' is provided we log to that location,
  # otherwise we buffer messages and log to stdout after its test completes.

  logging_buffer = queue.Queue()

  if args.logging_runlevel:
    if args.logging_path:
      handler = logging.FileHandler(args.logging_path, mode = 'w')
      handler.setLevel(stem.util.log.logging_level(args.logging_runlevel))
      handler.setFormatter(stem.util.log.FORMATTER)
    else:
      handler = logging.handlers.QueueHandler(logging_buffer)
      handler.setLevel(stem.util.log.logging_level(args.logging_runlevel))

    stem.util.log.get_logger().addHandler(handler)

  # filters for how testing output is displayed

  error_tracker = test.output.ErrorTracker()

  output_filters = (
    error_tracker.get_filter(),
    test.output.runtimes,
    test.output.strip_module,
    test.output.align_results,
    test.output.colorize,
  )

  # Number of tests that we have skipped. This is only available with python
  # 2.7 or later because before that test results didn't have a 'skipped'
  # attribute.

  # TODO: handling of earlier python versions is no longer necessary here
  # TODO: this invokes all asynchronous tests, even if we have a --test or
  #   --exclude-test argument

  skipped_tests = 0

  if args.run_integ:
    default_test_dir = stem.util.system.expand_path(CONFIG['integ.test_directory'], test.STEM_BASE)
    async_args = test.AsyncTestArgs(default_test_dir, args.tor_path)

    for module_str in stem.util.test_tools.ASYNC_TESTS:
      module = importlib.import_module(module_str.rsplit('.', 1)[0])
      test_classes = [v for k, v in module.__dict__.items() if k.startswith('Test')]

      if len(test_classes) != 1:
        print(
            f"BUG: Detected multiple tests for {module_str}: {', '.join(test_classes)}"
        )
        sys.exit(1)

      test_classes[0].run_tests(async_args)

  if args.run_unit:
    test.output.print_divider('UNIT TESTS', True)
    error_tracker.set_category('UNIT TEST')

    for test_class in get_unit_tests(args.specific_test, args.exclude_test):
      run_result = _run_test(args, test_class, args.exclude_test, output_filters)
      test.output.print_logging(logging_buffer)
      skipped_tests += len(getattr(run_result, 'skipped', []))

    println()

  if args.run_integ:
    test.output.print_divider('INTEGRATION TESTS', True)
    integ_runner = test.runner.get_runner()

    for target in args.run_targets:
      error_tracker.set_category(target)

      try:
        integ_runner.start(target, args.attribute_targets, args.tor_path)

        println('Running tests...\n', STATUS)

        for test_class in get_integ_tests(args.specific_test, args.exclude_test):
          run_result = _run_test(args, test_class, args.exclude_test, output_filters)
          test.output.print_logging(logging_buffer)
          skipped_tests += len(getattr(run_result, 'skipped', []))

          if not integ_runner.assert_tor_is_running():
            # our tor process died

            error_tracker.register_error()
            break
      except KeyboardInterrupt:
        println('  aborted starting tor: keyboard interrupt\n', ERROR)
        break
      except ValueError as exc:
        println(str(exc), ERROR)  # can arise if there's bad settings.cfg data
        break
      except OSError:
        error_tracker.register_error()
      finally:
        println()
        integ_runner.stop()
        println()

  # ensure that we join all our threads

  active_threads = threading.enumerate()

  if len(active_threads) > 1:
    println('Threads lingering after test run:', ERROR)

    for lingering_thread in active_threads:
      println(f'  {lingering_thread}', ERROR)

  static_check_issues = {}

  for task in (test.task.PYFLAKES_TASK, test.task.PYCODESTYLE_TASK, test.task.MYPY_TASK):
    if not task.is_available and task.unavailable_msg:
      println(task.unavailable_msg, ERROR)
    else:
      task.join()  # no-op if these haven't been run

      if task.result:
        for path, issues in task.result.items():
          for issue in issues:
            static_check_issues.setdefault(path, []).append(issue)

  _print_static_issues(static_check_issues)

  if error_tracker.has_errors_occured():
    println('TESTING FAILED (%i seconds)' % (time.time() - start_time), ERROR, STDERR)

    for line in error_tracker:
      println(f'  {line}', ERROR, STDERR)

    error_modules = error_tracker.get_modules()

    if len(error_modules) < 10 and not args.specific_test:
      println('\nYou can re-run just these tests with:\n', ERROR, STDERR)

      for module in error_modules:
        println(
            f"  {' '.join(sys.argv)} --test {test.arguments.crop_module_name(module)}",
            ERROR,
            STDERR,
        )
  else:
    if skipped_tests > 0:
      println('%i TESTS WERE SKIPPED' % skipped_tests, STATUS)

    println('TESTING PASSED (%i seconds)\n' % (time.time() - start_time), SUCCESS)

  if new_capabilities := test.get_new_capabilities():
    println(NEW_CAPABILITIES_FOUND, ERROR)

    for capability_type, msg in sorted(new_capabilities, key = lambda x: x[1]):
      println(f'  [{capability_type}] {msg}', ERROR)

  sys.exit(1 if error_tracker.has_errors_occured() else 0)


def _print_static_issues(static_check_issues):
  if not static_check_issues:
    return
  println('STATIC CHECKS', STATUS)

  for file_path in sorted(static_check_issues):
    println(f'* {file_path}', STATUS)

    # Make a dict of line numbers to its issues. This is so we can both sort
    # by the line number and clear any duplicate messages.

    line_to_issues = {}

    for issue in static_check_issues[file_path]:
      line_to_issues.setdefault(issue.line_number, set()).add((issue.message, issue.line))

    for line_number in sorted(line_to_issues.keys()):
      for msg, line in line_to_issues[line_number]:
        line_count = '%-4s' % line_number
        content = f' | {line.strip()}' if line.strip() else ''
        println('  line %s - %-40s%s' % (line_count, msg, content))

    println()


def _run_test(args, test_class, exclude, output_filters):
  # When logging to a file we don't have stdout's test delimiters to correlate
  # logs with the test that generated them.

  if args.logging_path:
    stem.util.log.notice(f'Beginning test {test_class}')

  start_time = time.time()

  # Test classes look like...
  #
  #   test.unit.util.conf.TestConf.test_parse_enum_csv
  #
  # We want to strip the 'test.unit.' or 'test.integ.' prefix since it's
  # redundant. We also want to drop the test class name. The individual test
  # name at the end it optional (only present if we used the '--test'
  # argument).

  label_comp = test_class.split('.')[2:]
  del label_comp[-1 if label_comp[-1][0].isupper() else -2]
  test_label = '  %-52s' % ('.'.join(label_comp) + '...')

  if args.verbose:
    test.output.print_divider(test_class)
  else:
    println(test_label, STATUS, NO_NL)

  try:
    suite = unittest.TestLoader().loadTestsFromName(test_class)
  except AttributeError:
    if not args.specific_test:
      raise
    # should only come up if user provided '--test' for something that doesn't exist
    println(' no such test', ERROR)
    return None
  except Exception as exc:
    println(' failed', ERROR)
    traceback.print_exc(exc)
    return None

  # check if we should skip any individual tests within this module

  if exclude:
    cropped_name = test.arguments.crop_module_name(test_class)
    cropped_name = cropped_name.rsplit('.', 1)[0]  # exclude the class name

    for prefix in exclude:
      if prefix.startswith(cropped_name):
        test_name = prefix.split('.')[-1]

        suite._tests = list(filter(lambda test: test.id().split('.')[-1] != test_name, suite._tests))

  test_results = io.StringIO()
  run_result = stem.util.test_tools.TimedTestRunner(test_results, verbosity = 2).run(suite)

  if args.verbose:
    println(test.output.apply_filters(test_results.getvalue(), *output_filters))
  elif not run_result.failures and not run_result.errors:
    println(' success (%0.2fs)' % (time.time() - start_time), SUCCESS)
  elif args.quiet:
    println(test_label, STATUS, NO_NL, STDERR)
    println(' failed (%0.2fs)' % (time.time() - start_time), ERROR, STDERR)
    println(test.output.apply_filters(test_results.getvalue(), *output_filters), STDERR)
  else:
    println(' failed (%0.2fs)' % (time.time() - start_time), ERROR)
    println(test.output.apply_filters(test_results.getvalue(), *output_filters), NO_NL)

  if args.logging_path:
    stem.util.log.notice(f'Finished test {test_class}')

  return run_result


if __name__ == '__main__':
  main()
