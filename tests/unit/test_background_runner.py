import threading
import time

from repodoctor.api.runner import BackgroundRunner


def wait_until(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_tasks_for_one_session_run_in_order_even_when_submitted_while_the_first_is_running():
    runner = BackgroundRunner(2, lambda sid, exc: None)
    order, release = [], threading.Event()

    def first():
        order.append("first-start")
        release.wait(5)
        order.append("first-end")

    runner.submit("s1", first)
    assert wait_until(lambda: "first-start" in order)
    runner.submit(
        "s1", lambda: order.append("second")
    )  # arrives while the first is still in flight
    assert runner.is_running("s1") and order == [
        "first-start"
    ]  # not run concurrently, not dropped
    release.set()
    assert wait_until(lambda: order == ["first-start", "first-end", "second"])
    assert wait_until(lambda: not runner.is_running("s1"))
    runner.shutdown()


def test_different_sessions_run_in_parallel():
    runner = BackgroundRunner(2, lambda sid, exc: None)
    barrier = threading.Barrier(2, timeout=5)
    done = []
    for sid in ("a", "b"):
        runner.submit(sid, lambda sid=sid: (barrier.wait(), done.append(sid)))
    assert wait_until(
        lambda: sorted(done) == ["a", "b"]
    )  # would deadlock if they ran one after the other
    runner.shutdown()


def test_an_error_is_reported_and_does_not_stop_later_tasks_or_the_worker():
    errors, ran = [], []
    runner = BackgroundRunner(1, lambda sid, exc: errors.append((sid, str(exc))))

    def boom():
        raise RuntimeError("kaput")

    runner.submit("s", boom)
    runner.submit("s", lambda: ran.append("after"))
    assert wait_until(lambda: ran == ["after"])
    assert errors == [("s", "kaput")]
    runner.submit(
        "s", lambda: ran.append("later")
    )  # the session slot was released and reusable
    assert wait_until(lambda: ran == ["after", "later"])
    runner.shutdown()


def test_a_failing_error_handler_does_not_kill_the_worker():
    def bad_handler(sid, exc):
        raise ValueError("handler is broken too")

    ran = []
    runner = BackgroundRunner(1, bad_handler)
    runner.submit("s", lambda: 1 / 0)
    runner.submit("s", lambda: ran.append("ok"))
    assert wait_until(lambda: ran == ["ok"])
    runner.shutdown()
