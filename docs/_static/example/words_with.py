import itertools
import re

from stem.util import term
from stem.util.term import Attr, Color


def get_words_with(target, attr):
  """
  Provides words with the given substring highlighted within it.

  :param str target: substring to match against
  :param tuple attr: terminal formatting to highlight the match with

  :returns: **iterable** with words containing that substring
  """

  word_matcher = re.compile(f'(.*)({target})(.*)', re.I)

  with open('/etc/dictionaries-common/words') as dictionary_file:
    for word in dictionary_file:
      if match := word_matcher.match(word):
        yield ''.join((
          match.group(1),
          term.format(match.group(2), *attr),
          match.group(3),
        ))


def main():
  target = input("What substring would you like to look for? We'll get words containing it: ")
  attr = (Attr.BOLD, Color.YELLOW)

  print("Words with '%s' include...\n" % term.format(target, *attr))

  for words in itertools.zip_longest(*(get_words_with(target, attr),) * 4):
    print('%-30s%-30s%-30s%-30s' % tuple(w if w else '' for w in words))


if __name__ == '__main__':
  main()
