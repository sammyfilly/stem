import stem.util.system
import time


def fibonacci(n):
  return n if n < 2 else fibonacci(n - 2) + fibonacci(n - 1)


def main():
  # calculate fibonacci sequences four times in parallel

  start_time, threads = time.time(), []

  threads.extend(
      stem.util.system.DaemonTask(fibonacci, (35, ), start=True)
      for _ in range(4))
  for t in threads:
    t.join()

  print('took %0.1f seconds' % (time.time() - start_time))


if __name__ == '__main__':
  main()
