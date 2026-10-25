# Chess

A Python-powered chess website. It keeps the existing browser board and uses a
Python/Flask server with SQLite for chess rules, accounts, friends, invitations,
and saved game rooms. People can play using a friend invitation or share a room
link with a guest who does not have an account.

## Run locally

From this folder, install the Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

Start the website:

```powershell
python app.py
```

Open [http://127.0.0.1:20202](http://127.0.0.1:20202). By default the local
server listens only on your own computer. Create an account to use
the friends list, or create a room and share its link. The other player can
join without registering. Game rooms and accounts are saved to `chess.sqlite3`.

Usernames must be 3–20 letters, numbers, or underscores. Passwords must be at
least 10 characters. Friend requests must be accepted before either player can
send a friend invitation. New accounts start at 800 Elo. For games between two
registered accounts, the server automatically updates both ratings after a
win, loss, draw, or forfeit using the standard Elo expected-score formula
(K-factor 32). Guest games do not change account ratings.

## Host on the internet

The app can run as a WSGI application on a Python hosting service. Wasmer's
available Python runtime, persistent storage, and web-service settings depend
on the deployment type you choose. Use a service/container setup that supports
a persistent writable disk for SQLite and forwards HTTPS traffic to the
application. Set these environment variables:

- `CHESS_SECRET_KEY`: a long, random, private value that remains stable between
  deploys.
- `CHESS_ENV=production`: enables secure session cookies by default.
- `CHESS_DATABASE`: the path to the SQLite database on the persistent disk.
- `CHESS_SECURE_COOKIES=true`: enables secure session cookies when served over
  HTTPS.
- `PORT`: the port supplied by the hosting provider.

For local use, the app binds to `127.0.0.1`. A hosting container must expose the
application port; if its start command runs `python app.py` directly, set
`CHESS_HOST=0.0.0.0`. Prefer the host's production WSGI process command where
available. For a Linux host with Gunicorn, install `requirements.txt` and start
with:

```sh
gunicorn --workers 1 --threads 8 --bind "0.0.0.0:$PORT" app:app
```

Keep the Flask development server off public production traffic. Confirm the
Wasmer deployment provides HTTPS, sets `CHESS_ENV=production`, gives the app
reliable persistent storage, and supplies a stable secret key. Secure cookies
require HTTPS. No external site has been deployed from this workspace.

The standalone Python desktop game is still available separately:

```powershell
python chess_gui.py
```
