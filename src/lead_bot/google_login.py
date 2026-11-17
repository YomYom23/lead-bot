# lead-bot/src/lead_bot/google_login.py
"""One-time Google sign-in for the calendar. Run it yourself, in a terminal:

    uv run python -m lead_bot.google_login

It opens your browser at Google's consent screen. Sign in with the account whose calendar takes
the bookings (it must be listed as a test user while the consent screen is in Testing). Google
will warn that the app isn't verified; continue past it. The sign-in is saved to
google_token.json (or GOOGLE_TOKEN_PATH), which the call server reads.

Run it again whenever the call server says "sign in to Google again". In Testing that's every 7
days. The server never opens a browser itself, so on a remote server: run this on your own computer and
copy google_token.json across.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .google_calendar import (
    DEFAULT_TOKEN_URL,
    GOOGLE_SCOPES,
    GoogleSignIn,
    google_token_path_from_environment,
)


def find_client_secret_file(folder: Path) -> Path:
    """The OAuth client file downloaded from Google Cloud Console.

    GOOGLE_CLIENT_SECRET_PATH wins if set. Otherwise look for Google's default download name
    (client_secret_<numbers>.apps.googleusercontent.com.json), and refuse to guess if there are
    several, since they could belong to different Google Cloud projects.
    """
    configured_path = os.environ.get("GOOGLE_CLIENT_SECRET_PATH", "").strip()
    if configured_path:
        return Path(configured_path)
    matches = sorted(folder.glob("client_secret*.json"))
    if len(matches) != 1:
        found = ", ".join(match.name for match in matches) or "none"
        raise SystemExit(
            f"Expected exactly one client_secret*.json in {folder} (found: {found}). "
            "Set GOOGLE_CLIENT_SECRET_PATH in .env to choose one."
        )
    return matches[0]


def save_google_sign_in(token_path: Path, sign_in: GoogleSignIn) -> None:
    """Write the sign-in where google_calendar.load_google_sign_in reads it."""
    token_path.write_text(
        json.dumps(
            {
                "refresh_token": sign_in.refresh_token,
                "client_id": sign_in.client_id,
                "client_secret": sign_in.client_secret,
                "token_url": sign_in.token_url,
                "scopes": list(GOOGLE_SCOPES),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    # Only the file's owner may read it. Matters on the Linux server; on Windows chmod can only
    # toggle read-only, and 0o600 leaves the file writable.
    token_path.chmod(0o600)


def main() -> None:
    load_dotenv()
    # Imported here, not at the top: only this command needs it, and it's the one dependency
    # without type information.
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_secret_path = find_client_secret_file(Path.cwd())
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), scopes=list(GOOGLE_SCOPES))
    try:
        # port=0: the operating system picks any free port for the local page Google redirects
        # back to, so nothing already running on 8080 gets in the way.
        credentials = flow.run_local_server(port=0, timeout_seconds=300)
    except Warning as error:
        # oauthlib raises a Warning when Google grants fewer permissions than were asked for.
        # That happens if a box is left unticked on the consent screen.
        raise SystemExit(
            f"Google didn't grant every permission the bot needs ({error}). Run this again and "
            "tick every box on the consent screen."
        ) from None

    if not credentials.refresh_token:
        raise SystemExit("Google didn't return a refresh token, so the bot couldn't stay signed in.")

    token_path = google_token_path_from_environment()
    save_google_sign_in(
        token_path,
        GoogleSignIn(
            refresh_token=str(credentials.refresh_token),
            client_id=str(credentials.client_id),
            client_secret=str(credentials.client_secret),
            # getattr: the library's type for these credentials doesn't always declare token_uri.
            token_url=str(getattr(credentials, "token_uri", "") or DEFAULT_TOKEN_URL),
        ),
    )
    print(f"Signed in. Saved to {token_path}. Start (or restart) the call server now.", file=sys.stderr)


if __name__ == "__main__":
    main()
