# Mixtape Bug Hunt — Submission

## AI Usage

*(To be completed in Milestone 4, after all bugs are fixed — this section will describe
specifically what I asked AI tools to explain/trace/summarize during navigation and
debugging, and where I verified or overrode their output.)*

---

## Codebase Map

### Main files and their roles

- **`app.py`** — Flask application factory (`create_app`). Initializes the `db` (Flask-SQLAlchemy)
  object, reads `DATABASE_URL`/`SECRET_KEY` from the environment (defaulting to a local
  SQLite file and a dev secret), registers the four blueprints under their URL prefixes
  (`/songs`, `/playlists`, `/users`, `/feed`), and calls `db.create_all()`. There's no
  `if __name__` production entrypoint beyond a `debug=True` dev runner — the project must be
  started via `flask run`, not `python app.py` (running it directly double-imports the app and
  breaks SQLAlchemy's model registry).

- **`models.py`** — All SQLAlchemy models, plus three association tables:
  - `User` — has `listening_streak` (int) and `last_listened_at` (datetime) columns used
    directly by the streak feature; a self-referential many-to-many `friends` relationship via
    the `friendships` table (`lazy="dynamic"`).
  - `Song` — has `shared_by` (the original sharer) and a many-to-many `tags` relationship via
    `song_tags`.
  - `ListeningEvent` — one row per "user listened to song" action; this is the source of truth
    both the streak feature and the friends-activity feed read from.
  - `Rating` — one row per (user, song) pair, enforced by a `UniqueConstraint`; re-rating
    updates the existing row rather than inserting a new one.
  - `Playlist` — many-to-many to `Song` via `playlist_entries`, which is not a plain join table:
    it carries `position` (explicit ordering, not insertion order), `added_by`, and `added_at`.
  - `Notification` — flat table: `user_id` (recipient), `notification_type`, `body`, `read`.
    There's no polymorphic/typed notification model — every notification type is just a string
    tag plus a pre-rendered body.
  - All primary keys are UUID strings (`generate_uuid()`), not auto-increment ints.

- **`routes/`** — one blueprint per resource area. Every handler follows the same shape: parse
  request args/JSON, call exactly one service function, and either `jsonify` the result or catch
  `ValueError` and return it as a 400/404. No business logic lives in routes.
  - `songs.py` — search, get-by-id, rate, record-a-listen.
  - `playlists.py` — create, get metadata, list songs, add a song.
  - `users.py` — get profile, get streak, list/mark-read notifications.
  - `feed.py` — friends-listening-now, activity feed.

- **`services/`** — all business logic, one module per feature area:
  - `streak_service.py` — `record_listening_event()` creates a `ListeningEvent` and calls
    `update_listening_streak()`, which compares `last_listened_at.date()` to `today` and
    increments/resets `listening_streak` based on the day delta.
  - `feed_service.py` — `get_friends_listening_now()` looks up `user.friends`, pulls
    `ListeningEvent`s for those friend IDs newer than a 24-hour cutoff, and dedupes to one
    (most recent) event per friend. `get_activity_feed()` is the non-time-filtered sibling —
    just the most recent N events from friends regardless of age.
  - `search_service.py` — `search_songs()` does a case-insensitive `ilike` match against
    `title`/`artist`, outer-joined to `song_tags` so tags can be included in the result shape.
    `get_song()` is a plain lookup-by-id.
  - `notification_service.py` — `create_notification()` is the generic writer every notification
    type is supposed to funnel through. `add_to_playlist()` appends a song to a playlist and,
    if the adder isn't the original sharer, calls `create_notification(..., "song_added_to_playlist", ...)`.
    `rate_song()` upserts a `Rating` row but does not call `create_notification()` anywhere.
    `get_notifications()` / `mark_as_read()` are read/update helpers used by `routes/users.py`.
  - `playlist_service.py` — `create_playlist()`, `get_playlist()`, `get_user_playlists()` are
    straightforward CRUD/read helpers. `get_playlist_songs()` queries `Song` joined through
    `playlist_entries`, ordered ascending by `position`.

- **`seed_data.py`** — drops and recreates all tables, then inserts 5 users with a friendship
  graph, 25 songs split into three groups (0, 1, and 3+ tags per song — the multi-tag group is
  called out in a comment as relevant to Issue #3), recent vs. old `ListeningEvent`s (relevant to
  Issue #2's 24h cutoff), pre-set `last_listened_at`/`listening_streak` values per user, three
  playlists of 5–7 songs each, and one pre-existing "song added to playlist" notification (so the
  *working* notification pattern is visible in seeded data when investigating Issue #4).

- **`tests/`** — `test_streaks.py`, `test_search.py`, `test_playlists.py`, each using an
  in-memory SQLite fixture (`app` fixture creates/drops all tables per test). These exercise
  services directly (not through HTTP), and some assertions describe the *intended* correct
  behavior rather than current behavior — e.g. `test_streaks.py::test_streak_increments_on_sunday`
  currently fails against `streak_service.py` as written.

### Data flow — adding a friend's shared song to a playlist triggers a notification

1. Client sends `POST /playlists/<playlist_id>/songs` with `{song_id, added_by}` in the body.
2. `routes/playlists.py::add_song()` validates both fields are present, then calls
   `notification_service.add_to_playlist(playlist_id, song_id, added_by)`.
3. Inside `add_to_playlist()`:
   - Looks up the `Song`, the adding `User`, and the `Playlist` — raises `ValueError` (→ 400 in
     the route) if any is missing.
   - If the song isn't already in `playlist.songs`, appends it (this writes a `playlist_entries`
     row via the SQLAlchemy `secondary` relationship) and commits.
   - If `song.shared_by != added_by_user_id` (i.e., you didn't add your own song), calls
     `create_notification(user_id=song.shared_by, notification_type="song_added_to_playlist", body=...)`.
4. `create_notification()` builds a `Notification` row and commits it.
5. The route returns `{"message": "Song added to playlist"}`, 201.
6. Later, the recipient fetches `GET /users/<user_id>/notifications` →
   `routes/users.py::notifications()` → `notification_service.get_notifications()`, which
   queries `Notification` by `user_id` (optionally filtered to unread) ordered by
   `created_at` descending.

Note this is the *only* place `create_notification()` is currently called from — rating a song
(`notification_service.rate_song()`, step 3 above's sibling function) goes through the same
module but never reaches `create_notification()`, which is directly relevant to Issue #4.

### Patterns noticed

- **Strict route → service → model layering.** Every route function is ~5–10 lines: parse,
  delegate, respond. All conditionals and queries live in `services/`. This makes each service
  function testable in isolation (see how `tests/` calls services directly, bypassing Flask).
- **Consistent error convention.** Services raise `ValueError` with a human-readable message for
  not-found/invalid-input; routes catch it and forward the message as JSON with the right status
  code. No custom exception hierarchy.
- **Association tables carry extra state beyond the FK pair.** Both `playlist_entries`
  (`position`, `added_by`, `added_at`) and the implicit ordering assumptions in
  `feed_service`/`streak_service` around timestamps suggest most of the app's business rules are
  really about *time and order*, not just data existence — which lines up with 3 of the 5 known
  issues being about date/time boundaries (streak, feed recency, playlist ordering).
- **Notifications are a single flat, stringly-typed table** with one generic writer function
  (`create_notification`). Every feature that wants to notify someone is expected to call it
  explicitly — there's no automatic hook or event system, so adding a new notification trigger
  is a manual, easy-to-forget step per feature.
