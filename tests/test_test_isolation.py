"""Temporary test artifacts are removed on normal and failing teardown paths."""

from pathlib import Path
import socket

import pytest

from conftest import tmp_path as temporary_path_fixture
from logic.transcript import GameTranscript


@pytest.mark.parametrize("failed", [False, True])
def test_temporary_artifacts_are_deleted_on_teardown(failed):
    """Exercise the fixture finalizer with nested output and simulated test failure."""
    fixture = temporary_path_fixture.__wrapped__()
    directory = next(fixture)
    logs = directory / ".logged_games"
    logs.mkdir()
    (logs / "session.html").write_text("fake transcript", encoding="utf-8")
    if failed:
        with pytest.raises(RuntimeError, match="simulated test failure"):
            fixture.throw(RuntimeError("simulated test failure"))
    else:
        with pytest.raises(StopIteration):
            next(fixture)
    assert not directory.exists()


def test_default_transcripts_stay_in_disposable_working_directory(tmp_path):
    """Default application paths cannot write game artifacts into the repository."""
    transcript = GameTranscript()
    assert Path.cwd() == tmp_path
    assert transcript.log_dir.resolve() == tmp_path / ".logged_games"


def test_network_guard_blocks_external_and_backend_transports(request):
    if request.config.getoption("--allow-network"):
        pytest.skip("Network guard explicitly disabled")
    with pytest.raises(AssertionError, match="network access is forbidden"):
        socket.getaddrinfo("api.openai.com", 443)
    with socket.socket() as connection:
        with pytest.raises(AssertionError, match="network access is forbidden"):
            connection.connect(("127.0.0.1", 8080))
        with pytest.raises(AssertionError, match="network access is forbidden"):
            connection.connect_ex(("127.0.0.1", 8080))
    with socket.socket(type=socket.SOCK_DGRAM) as connection:
        with pytest.raises(AssertionError, match="network access is forbidden"):
            connection.sendto(b"probe", ("127.0.0.1", 8080))
