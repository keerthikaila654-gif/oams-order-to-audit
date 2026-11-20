import os, re, sys
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app import create_app, reset_database
from app.db import connect


@pytest.fixture()
def app(tmp_path):
    a = create_app({"DATABASE": str(tmp_path / "t.db"), "TESTING": True, "SECRET_KEY": "test"})
    reset_database(a.config["DATABASE"])
    return a


@pytest.fixture()
def conn(app):
    c = connect(app.config["DATABASE"])
    yield c
    c.close()


@pytest.fixture()
def login(app):
    def _login(user):
        c = app.test_client()
        tok = re.search(r'name="_csrf" value="(\w+)"', c.get("/login").get_data(True)).group(1)
        r = c.post("/login", data={"username": user, "password": "Pass@123", "_csrf": tok})
        assert r.status_code == 302
        c.csrf = lambda: re.search(r'csrf-token" content="(\w+)"', c.get(c.get("/").headers["Location"]).get_data(True)).group(1)
        return c
    return _login
