"""Exclusive bounded archive creation and failed-start cleanup."""

import asyncio

import pytest

from logic.transcript import GameTranscript


def test_concurrent_archives_reserve_distinct_bounded_filenames(tmp_path):
    async def run():
        archives = [GameTranscript(tmp_path) for _ in range(4)]
        await asyncio.gather(*(archive.start("x" * 1000, "Opening") for archive in archives))
        assert len({archive.path for archive in archives}) == 4
        assert all(len(archive.path.name) < 110 for archive in archives)
        await asyncio.gather(*(archive.finalize() for archive in archives))
        assert all(
            archive.path.read_text(encoding="utf-8").endswith("</html>\n") for archive in archives
        )

    asyncio.run(run())


def test_failed_archive_creation_can_be_retried(tmp_path, monkeypatch):
    async def run():
        archive = GameTranscript(tmp_path)
        original = archive._create

        def fail(*args):
            raise OSError("Disk full")

        monkeypatch.setattr(archive, "_create", fail)
        with pytest.raises(OSError):
            await archive.start("Quest", "Rejected opening")
        assert archive.path is None
        monkeypatch.setattr(archive, "_create", original)
        await archive.start("Quest", "Accepted opening")
        text = archive.path.read_text(encoding="utf-8")
        assert "Accepted opening" in text
        assert "Rejected opening" not in text
        await archive.finalize()

    asyncio.run(run())
