import threading
import time


def fibonacci(n):
  return n if n < 2 else fibonacci(n - 2) + fibonacci(n - 1)


def main():
  # calculate fibonacci sequences four times in parallel

  start_time, threads = time.time(), []

  for _ in range(4):
    t = threading.Thread(target = fibonacci, args = (35,))
    t.daemon = True
    t.start()

    threads.append(t)

  for t in threads:
    t.join()

  print('took %0.1f seconds' % (time.time() - start_time))


if __name__ == '__main__':
  main()
