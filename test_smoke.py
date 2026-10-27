"""Test de fumée : connexion + ouverture de chaque page pour chaque profil."""
import os
import tempfile

os.environ["SCREENLEAD_DB"] = tempfile.mktemp(suffix=".db")

from streamlit.testing.v1 import AppTest  # noqa: E402

ACCOUNTS = [("moa", "Moa#2026"), ("chef1", "Chef1#2026"), ("ing1", "Ing1#2026"), ("admin", "Admin#2026")]


def new_app():
    return AppTest.from_file("app.py", default_timeout=120)


def test_bad_login():
    at = new_app().run()
    at.text_input[0].set_value("moa")
    at.text_input[1].set_value("mauvais")
    at.button[0].click()
    at.run()
    assert not at.exception
    assert any("incorrect" in e.value for e in at.error)


def test_all_pages_all_roles():
    for user, pwd in ACCOUNTS:
        at = new_app().run()
        at.text_input[0].set_value(user)
        at.text_input[1].set_value(pwd)
        at.button[0].click()
        at.run()
        assert not at.exception, (user, at.exception)
        radio = at.sidebar.radio[0]
        keys = list(radio.options)
        print(user, "->", keys)
        for i in range(len(keys)):
            at.sidebar.radio[0].set_index(i)
            at.run()
            assert not at.exception, (user, keys[i], at.exception)


def test_permissions_isolated():
    at = new_app().run()
    at.text_input[0].set_value("chef1")
    at.text_input[1].set_value("Chef1#2026")
    at.button[0].click()
    at.run()
    opts = " ".join(at.sidebar.radio[0].options)
    assert "Utilisateurs" not in opts and "Chantiers" not in opts
