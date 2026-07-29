"""テスト全体の共通設定。

テストは「NEUTRINOが設定済みのサーバー」を前提にする。/api/jobs は
NEUTRINO_ROOT 未設定のサーバーに synthesizer=neutrino(フォームの既定値)が
来たら422で弾くので、既定のまま投入するテストのためにダミーのルートを入れておく。
未設定側の挙動を見るテストは、各自 monkeypatch.delenv で外すこと。
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _neutrino_root_env(monkeypatch, tmp_path_factory):
    if not os.environ.get("NEUTRINO_ROOT"):
        monkeypatch.setenv(
            "NEUTRINO_ROOT", str(tmp_path_factory.mktemp("neutrino-root"))
        )
