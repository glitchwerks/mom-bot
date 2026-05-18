"""AAD-token injection for Postgres connections."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from mom_bot.db import build_session_factory


def test_sqlite_url_does_not_invoke_token_hook() -> None:
    """Local-dev SQLite path must NOT acquire AAD tokens."""
    with patch("mom_bot.db.ManagedIdentityCredential") as mic:
        factory = build_session_factory("sqlite:///:memory:")
        # Open a session to actually establish a connection.
        with factory() as s:
            s.execute(__import__("sqlalchemy").text("select 1"))
        mic.assert_not_called()


def test_postgres_url_injects_token_as_password() -> None:
    """Postgres path must call ManagedIdentityCredential.get_token and stamp the password."""
    fake_token = MagicMock(token="FAKE-AAD-TOKEN-abc")
    with (
        patch("mom_bot.db.ManagedIdentityCredential") as mic_cls,
        patch("mom_bot.db.create_engine") as ce,
    ):
        mic_cls.return_value.get_token.return_value = fake_token
        engine = MagicMock()
        ce.return_value = engine
        # Capture the do_connect listener.
        listeners: list = []
        engine.dispatch = MagicMock()

        def fake_listen(target, name, fn):
            listeners.append((name, fn))

        def _fake_listens_for(*a: object, **kw: object):  # type: ignore[no-untyped-def]
            return lambda f: (listeners.append(("do_connect", f)), f)[1]

        with patch("mom_bot.db.event.listens_for") as lf:
            lf.side_effect = _fake_listens_for
            build_session_factory(
                "postgresql+psycopg://mi-mom-bot@srv.postgres.database.azure.com/mom_bot?sslmode=require",
                aad_client_id="11111111-2222-3333-4444-555555555555",
            )
        # Invoke the captured do_connect listener with a stub cparams dict.
        do_connect = next(fn for name, fn in listeners if name == "do_connect")
        cparams: dict[str, object] = {}
        do_connect(dialect=None, conn_rec=None, cargs=(), cparams=cparams)
        assert cparams["password"] == "FAKE-AAD-TOKEN-abc"
        mic_cls.return_value.get_token.assert_called_once_with(
            "https://ossrdbms-aad.database.windows.net/.default"
        )
