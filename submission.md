# Mixtape Bug Hunt — Submission

## AI Usage

I used Claude Code (Anthropic's CLI agent) throughout this project, directing it at each
milestone rather than asking it to "find and fix the bugs" outright.

- **Codebase orientation:** Had it read every file in `routes/`, `services/`, `models.py`,
  `seed_data.py`, and the existing tests, then write the codebase map and trace the
  add-to-playlist → notification data flow. I reviewed the map for accuracy against the actual
  files rather than taking it at face value.
- **Reproduction:** For each issue, had it write small standalone Python scripts that imported
  the relevant service function directly and called it with controlled inputs (the same
  "isolate the function in a shell" technique suggested in the project brief), rather than
  asking it to guess the bug from reading the code alone.
- **Issue #2 specifically:** My first hypothesis (mine and the AI's) was a timezone/naive-vs-aware
  datetime bug, since SQLite silently drops `tzinfo` on round-trip. That turned out to be a red
  herring — printing the compiled SQL with literal binds showed the filter was evaluating
  correctly at whatever threshold it was given. The AI caught this by checking the raw SQL
  before concluding, rather than stopping at the first plausible-looking theory.
- **Issue #3 specifically:** This is the one case where the "obvious" bug theory (a join fan-out
  producing duplicate rows) did not empirically reproduce. Rather than accept the theory because
  it matched the existing test's comment, the AI verified with raw SQL execution vs. ORM-level
  results, and found SQLAlchemy's legacy `Query.all()` auto-deduplicates by primary key. I asked
  it to keep digging for an alternate trigger before accepting this conclusion; it checked several
  more angles (session wrapping, pagination) and I agreed with its final call to document this as
  an investigation rather than force a fix for behavior that isn't currently broken.
- **Fix verification:** For every fix, it re-ran the full test suite plus targeted manual checks
  (e.g., self-rating should not self-notify; the activity feed should be unaffected by the
  feed-threshold change) before I considered the fix done. I reviewed each diff before it was
  committed.

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

---

## Root Cause Analysis

### Issue #1 — My listening streak keeps resetting

**How I reproduced it:** Called `update_listening_streak()` directly on a fresh user with a
Saturday timestamp, then a consecutive Sunday timestamp (same scenario as the existing
`tests/test_streaks.py::test_streak_increments_on_sunday`). Streak went to 1 after Saturday as
expected, but stayed at 1 after Sunday instead of incrementing to 2. Ran the full test suite —
that one test failed before any code changes.

**How I found the root cause:** `streak_service.py` is short, so I read
`update_listening_streak()` top to bottom against its own docstring, which states the rule as
simply "if the user listened yesterday: streak increments by 1." The actual code read:
`elif days_since_last == 1 and today.weekday() != 6:` — an extra condition not mentioned
anywhere in the docstring. Confirmed `datetime.weekday()` returns `6` for Sunday.

**The root cause:** The consecutive-day increment branch required both a 1-day gap *and*
`today.weekday() != 6`. Any consecutive-day listen that happened to land on a Sunday failed
that second condition and fell through to the `else` branch, resetting the streak to 1 even
though the user hadn't skipped a day.

**My fix and side-effect check:** Removed the `and today.weekday() != 6` clause, leaving
`elif days_since_last == 1:` as the sole condition for incrementing, matching the documented
rule. Verified `test_streak_increments_on_sunday` now passes, and re-ran the other three streak
tests (starts-at-1, same-day no double-count, skip-a-day resets) to confirm none regressed.

### Issue #2 — Friends Listening Now shows people from yesterday

**How I reproduced it:** Called `get_friends_listening_now()` for every seeded user and printed
each returned event's age. Found friends showing up as "listening now" from as far back as
~18 hours ago — e.g. darius and simone both saw nova's listening event from 2 hours prior.

**How I found the root cause:** `feed_service.py` defines `RECENT_THRESHOLD = timedelta(hours=24)`
and filters `ListeningEvent.listened_at >= cutoff` using it. I first suspected a timezone
bug (SQLite drops tzinfo on round-trip — confirmed this separately by inspecting raw column
values), but printing the compiled SQL with literal binds and cross-checking every event's
inclusion against the filter showed the query itself was filtering exactly at the 24-hour mark,
correctly. The threshold value itself was the problem. Cross-referencing `seed_data.py`'s own
comments — "Recent events (within the past 30 minutes) — should appear," "Older events...
should NOT appear" — confirmed the intended recency window is far tighter than 24 hours.

**The root cause:** `RECENT_THRESHOLD` was set to 24 hours, appropriate for a daily digest but
not for a "currently listening" feed. Any friend who listened at any point in the last day
qualified as "listening now," so someone who listened yesterday afternoon would still appear as
if they were listening right now.

**My fix and side-effect check:** Changed `RECENT_THRESHOLD` to `timedelta(minutes=30)`,
matching the boundary implied by the seed data comments. Verified friends whose only event is
2+ hours old no longer appear, while friends with a sub-20-minute-old event still do. Checked
`get_activity_feed()` (the non-recency-filtered sibling feature that shares this module) and
confirmed it was unaffected — it still returns all 8 events for nova's friends since it doesn't
use `RECENT_THRESHOLD` at all.

### Issue #4 — I got notified about a playlist add but not a rating

**How I reproduced it:** Called `rate_song()` with a `user_id` different from the song's
`shared_by`, then checked `get_notifications(sharer_id)` before and after — the count stayed at
0 both times, with no error raised.

**How I found the root cause:** Per the assignment hint, compared `rate_song()` line-by-line
against the working `add_to_playlist()` in the same file. `add_to_playlist()` ends with:
`if song.shared_by != added_by_user_id: create_notification(...)`. `rate_song()` has no
equivalent block at all — it upserts the `Rating` row, commits, and returns immediately.

**The root cause:** `notification_service.py` has a single generic `create_notification()`
writer that every feature must explicitly call to trigger a notification — there's no
automatic hook. `add_to_playlist()` was wired up to call it; `rate_song()` simply never was.
This is a missing integration, not a broken conditional — the architecture requires every
notification-worthy action to remember to call the writer, and this one didn't.

**My fix and side-effect check:** Added the same shaped guard to the end of `rate_song()` —
`if song.shared_by != user_id: create_notification(...)` — using a new `"song_rated"`
notification type and a body message parallel to the existing `"song_added_to_playlist"` one.
Verified rating a friend's song now produces exactly one notification, and rating your own song
produces zero (no self-notification), mirroring the guard already used in `add_to_playlist()`.
Added `tests/test_notifications.py` covering both cases, since this module had no test coverage
at all before this fix.

### Issue #5 — The last song in a playlist never shows up

**How I reproduced it:** Called `get_playlist_songs()` on a seeded 7-song playlist and compared
the returned count (6) against the playlist's actual song count (7). Also ran the existing
tests — `test_playlist_returns_all_songs` and `test_playlist_returns_songs_in_order` both
failed before any code changes.

**How I found the root cause:** `playlist_service.py` is short. `get_playlist_songs()` builds a
correctly-ordered query (`order_by(asc(playlist_entries.c.position))`) and then does
`[song.to_dict() for song in songs[:-1]]` — the `[:-1]` slice stood out immediately, since it
directly contradicts the function's own docstring note: "This function returns all songs in
the playlist."

**The root cause:** The list comprehension sliced off the last element of the
already-correctly-ordered `songs` list before converting to dicts, unconditionally dropping the
final song of every playlist regardless of size.

**My fix and side-effect check:** Removed the `[:-1]` slice so the full ordered list is
returned. Verified both previously-failing tests now pass (all 5 songs returned, in
`Track 1..5` order), and re-checked `test_empty_playlist_returns_empty_list` still passes —
confirming no new off-by-one at the empty-list boundary.

### Issue #3 — The same song keeps showing up twice in search (investigated, not reproducible)

**How I attempted to reproduce it:** Called `search_songs()` directly against the seeded
3-tag song ("Crown Heights Anthem") and with broad queries matching up to 13 songs at once,
checking for any repeated song `id` in the results. Every attempt returned zero duplicates.
Ran the existing test suite — `test_search_no_duplicates_multi_tag_song` (whose own comment
reads "Should be 1, bug causes it to be 3") currently **passes**.

**Investigation:** `search_service.py` does `db.session.query(Song).outerjoin(song_tags,
Song.id == song_tags.c.song_id).filter(...).all()`. Compiling and running the raw SQL directly
(bypassing the ORM's row processing) confirmed the join does fan out to 3 raw rows for a song
with 3 tags, as the bug theory predicts. But `db.session.query(Song)...all()` is SQLAlchemy's
legacy `Query` API, which automatically de-duplicates full-entity results by primary key before
returning them — confirmed by checking `id()` of the returned Python objects (a single object,
not three references to the same object). This auto-dedup behavior applies regardless of query
string, tag count, or whether the join is inner or outer, so there's no reachable condition
through this function, as currently written, that produces a visible duplicate with the
installed `sqlalchemy==2.0.51` (matching the `sqlalchemy>=2.0.0` pin in `requirements.txt`).

**Conclusion:** No code change made for this issue. The join-fanout theory is correct at the
raw-SQL level, but it's absorbed by the ORM's built-in de-duplication before it ever reaches a
caller — the existing regression test already documents and passes this. I'm noting this as a
documented investigation rather than a fix, since I don't have evidence the described behavior
is currently reachable.
