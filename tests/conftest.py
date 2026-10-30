from pathlib import Path

import pytest

from csv_cleaner import create_app


@pytest.fixture
def sample_bytes():
    return (Path(__file__).parents[1] / "samples/fictional_customers.csv").read_bytes()


@pytest.fixture
def app():
    return create_app({"TESTING": True, "SECRET_KEY": "test-only-key"})


@pytest.fixture
def client(app):
    return app.test_client()
