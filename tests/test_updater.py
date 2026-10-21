import hashlib

import pytest

from whyslow import updater
from whyslow.updater import Release, UpdateError

NAME = "WhySlow-Linux-x86_64.AppImage"
OLD, NEW = b"old build", b"new build"


def _sum(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class Checked(Exception):
    pass


@pytest.fixture
def app(tmp_path, monkeypatch):
    target = tmp_path / "app" / NAME
    target.parent.mkdir()
    target.write_bytes(OLD)
    monkeypatch.setattr(updater, "app_path", lambda: target)
    monkeypatch.setattr(updater, "asset_name", lambda: NAME)
    monkeypatch.setattr(updater, "IS_WINDOWS", False)
    monkeypatch.setattr(updater, "IS_MAC", False)
    served = {"url-new": NEW, "url-old": NEW}  # старой сборки на сайте уже нет
    monkeypatch.setattr(updater, "_download", lambda url, dest: dest.write_bytes(served[url]))

    def selfcheck(binary):  # до проверки запуском дошли — значит, сумма сошлась
        raise Checked(binary.read_bytes())
    monkeypatch.setattr(updater, "_selfcheck_binary", selfcheck)
    return monkeypatch


def test_reuploaded_asset_is_fetched_again(app):
    rel = Release("9.9.9", "v9.9.9", assets={NAME: ("url-old", _sum(OLD))})
    app.setattr(updater, "latest", lambda: Release("9.9.9", "v9.9.9", assets={NAME: ("url-new", _sum(NEW))}))
    with pytest.raises(Checked) as e:
        updater._install_app(rel, lambda _t: None)
    assert e.value.args[0] == NEW


def test_damaged_file_still_refused(app):
    rel = Release("9.9.9", "v9.9.9", assets={NAME: ("url-old", _sum(OLD))})
    app.setattr(updater, "latest", lambda: rel)  # на сайте та же сумма — файл правда битый
    with pytest.raises(UpdateError, match="контрольная сумма"):
        updater._install_app(rel, lambda _t: None)
