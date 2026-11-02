# Deprecations

Review this list now and then (suggested: first of each month). An item is removed once its **earliest removal** date has passed and its **removal condition** holds. When removed, delete its row and add a line to the changelog of the affected projects.

| Item | Deprecated | Earliest removal | Removal condition |
|---|---|---|---|
| `user.story_sort` / `user.story_order` and the `story_sort` / `story_order` fields of `GET/PATCH /api/auth/me` | 2026-10-25 | 2027-02-25 | No client older than the Apple release that stopped sending them is still in use (check the `User-Agent` / diagnostics of connected clients). |

## Removal steps

### `story_sort` / `story_order`

State today: the server ignores them when listing stories (each story view carries its own sort). The Apple app no longer reads or writes them. The web app only declares them in `frontend/src/lib/types.ts` and `api.ts`. They are still returned by `/auth/me`, accepted by `PATCH /auth/me`, and parsed by `ServerWorkspaceProfile`.

1. **Server**: drop the two columns with an Alembic migration (`backend/src/app/models/__init__.py`), the fields in `api/schemas.py`, and the assignments in `api/auth.py`. Remove the `("user", "story_sort")` entries in `main.py`'s legacy-column checks only if the migration chain no longer needs them.
2. **Web**: remove `story_sort` / `story_order` from `frontend/src/lib/types.ts` and `api.ts`.
3. **Apple**: remove `sort` / `order` from `ServerWorkspaceProfile` (`NewsGatorShared/.../ServerWorkspaceModels.swift`), the `story_sort` / `story_order` defaults in `LocalWorkspaceSettings.swift`, and the `sort:` / `order:` arguments of `updateFallbackSelection` callers (the app, `NewsGatorQA`, tests); use `StoryPresetsStore.fallbackSort` instead.
4. **Docs**: delete the deprecation sentence in `APPLE_CLIENT_COMPATIBILITY.md` and the note in `.github/copilot-instructions.md`, then this row.
5. Run the backend tests, `npm run check`, and the Apple Shared and QA tests.
