"""
tests/test_notifications.py — Mixtape

Tests for notification creation logic.
"""

import pytest
from app import create_app, db
from models import User, Song
from services.notification_service import rate_song, get_notifications


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:"})
    with app.app_context():
        db.create_all()
        yield app
        db.drop_all()


@pytest.fixture
def seed_song(app):
    """Create a sharer, a friend, and a song shared by the sharer."""
    with app.app_context():
        sharer = User(username="sharer", email="sharer@example.com")
        rater = User(username="rater", email="rater@example.com")
        db.session.add_all([sharer, rater])
        db.session.flush()

        song = Song(title="Neon City", artist="Late Bloom", shared_by=sharer.id)
        db.session.add(song)
        db.session.commit()

        yield {"sharer": sharer, "rater": rater, "song": song}


def test_rating_a_song_notifies_the_sharer(app, seed_song):
    """
    Rating a friend's shared song should notify the person who shared it.
    """
    with app.app_context():
        sharer = seed_song["sharer"]
        rater = seed_song["rater"]
        song = seed_song["song"]

        assert get_notifications(sharer.id) == []

        rate_song(rater.id, song.id, 5)

        notifications = get_notifications(sharer.id)
        assert len(notifications) == 1
        assert notifications[0]["type"] == "song_rated"


def test_rating_your_own_song_does_not_notify_you(app, seed_song):
    """Rating your own shared song should not create a self-notification."""
    with app.app_context():
        sharer = seed_song["sharer"]
        song = seed_song["song"]

        rate_song(sharer.id, song.id, 4)

        assert get_notifications(sharer.id) == []
