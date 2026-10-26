import multiprocessing

import pytest

from infomentor_mcp.storage.lock import SessionLock, SessionLockTimeout


def _acquire_in_process(path, connection):
    lock = SessionLock(path)
    try:
        with lock.acquire(timeout=0.15):
            connection.send("acquired")
    except SessionLockTimeout:
        connection.send("busy")
    finally:
        connection.close()


def _hold_in_process(path, connection):
    with SessionLock(path).acquire():
        connection.send("acquired")
        connection.recv()


def test_process_waits_and_times_out_while_lock_is_held(tmp_path):
    path = tmp_path / "session.lock"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_acquire_in_process, args=(path, child))
    with SessionLock(path).acquire():
        process.start()
        assert parent.poll(10)
        assert parent.recv() == "busy"
    process.join(10)
    assert process.exitcode == 0
    with SessionLock(path).acquire(timeout=0):
        assert path.stat().st_mode & 0o777 == 0o600


def test_nested_store_operations_do_not_deadlock(tmp_path):
    first = SessionLock.shared(tmp_path / "session.lock")
    second = SessionLock.shared(tmp_path / "session.lock")
    with first.acquire(timeout=0), second.acquire(timeout=0):
        assert first is second


def test_lock_releases_on_exception(tmp_path):
    path = tmp_path / "session.lock"
    with pytest.raises(ValueError), SessionLock(path).acquire():
        raise ValueError("synthetic error")
    with SessionLock(path).acquire(timeout=0):
        pass


def test_os_releases_lock_when_process_is_terminated(tmp_path):
    path = tmp_path / "session.lock"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_hold_in_process, args=(path, child))
    process.start()
    try:
        assert parent.poll(10)
        assert parent.recv() == "acquired"
        with pytest.raises(SessionLockTimeout), SessionLock(path).acquire(timeout=0.05):
            pass
    finally:
        process.terminate()
        process.join(10)
        parent.close()
        child.close()
    with SessionLock(path).acquire(timeout=0.2):
        pass
