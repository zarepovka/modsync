import requests
import pytest

from modsync.downloader import Downloader
from modsync.exceptions import DownloadError
from modsync.models import Mod


class FailingSession:
    def get(self, *args, **kwargs):
        raise requests.ConnectionError("network unavailable")


def test_download_failure_is_wrapped(tmp_path):
    downloader = Downloader(session=FailingSession())
    mod = Mod(name="OfflineMod", version="1", url="https://example.com/mod.zip")

    with pytest.raises(DownloadError, match="Could not download OfflineMod"):
        downloader.download(mod, tmp_path)
