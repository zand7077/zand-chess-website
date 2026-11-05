# Chess

A Python-powered chess website. It keeps the existing browser board and uses a
Python/Flask server with SQLite for local development, or Wasmer's managed
PostgreSQL/MySQL database when deployed. Accounts, friends, invitations, and
saved game rooms are stored in the configured database. People can play using a
friend invitation or share a room link with a guest who does not have an account.

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
join without registering. Local accounts and game rooms are saved to
`chess.sqlite3`.

The lobby displays the top 10 accounts by Elo. Select a player to view their
rating, account age, match record, and recent completed games, or send a friend
request.

Friend invitations stay in the lobby after they are sent, so you can keep
using the site while your friend decides when to join. The room remains in
**Your games**, where you can return to it or close it. Invitation colors are
randomly assigned. The community tabs also list active live matches; choose
**Spectate** to watch a game without joining it or affecting either player's
clock.

Open **Global chat** from the lobby header to talk with other players. Chat is
readable without an account; sign in to post, add a player as a friend, or send
them a game invitation from their message. Game rooms also include a private
chat log with animated typing indicators and speech bubbles above each
player's avatar. On phones, use the floating **Chat** button to open the game
chat without scrolling; one-tap quick messages are available there as well.

Usernames must be 3–20 letters, numbers, or underscores. Passwords must be at
least 10 characters. Friend requests must be accepted before either player can
send a friend invitation. New accounts start at 800 Elo. For games between two
registered accounts, the server automatically updates both ratings after a
win, loss, draw, flag, or forfeit using the standard Elo expected-score formula
(K-factor 32). Guest games do not change account ratings.

Games offer bullet, blitz, and rapid clocks with per-player time and Fischer
increments. White's clock starts on White's first move. After that, clocks pause
when either player leaves; a short heartbeat grace period covers temporary
connection interruptions. Each player's clock is enforced by the server; a
player who runs out of time loses. Boards stay oriented from each player's
perspective instead of flipping every turn. Either player can request a rematch
after a game; players can queue a premove during the opponent's turn, which is
submitted automatically and validated when their turn begins. The
new game begins only when the other player accepts.

Move grades and position evaluations use the Stockfish 19 Lite single-threaded
WebAssembly engine in the browser. The engine assets are served from `public/`,
so they do not require runtime downloads or a Stockfish process on the Wasmer
server. Near-best moves that offer an opponent a legal capture of a non-pawn
piece are highlighted as Brilliant. First-use engine loading may take a moment,
and analysis uses the player's device. Stockfish.js is GPL-3.0; its license
and authors are in `public/engine/Copying.txt` and `public/engine/AUTHORS.txt`.
The corresponding source is available at
[Stockfish.js v19.0.1](https://github.com/nmrugg/stockfish.js/tree/v19.0.1).

After a game, either player can open its review, step through or play back the
moves, and read Stockfish's grade, evaluation loss, and preferred move for each
analyzed turn. The review also highlights other top Stockfish continuations on
the board and explains their evaluation and tactical or positional benefits.

You can also play against browser-based Stockfish opponents at approximately
200 or 1000 Elo, or against Karti at approximately 1600 Elo. These strengths
are approximations, and bot games do not change account ratings. During a
friend invitation, the lobby reports when the invited player is present and
offers a shortcut back to the waiting room. The board supports animated
click-and-drag moves and a numbered queue of premoves that can be cancelled
individually.

## Host on the internet

The app detects Wasmer's managed PostgreSQL or MySQL database from its injected
`DB_*` connection variables and initializes the required tables on startup.
Wasmer automatically installs Python dependencies from `requirements.txt` and
detects Flask from `app.py`; connect the GitHub repository's `main` branch and
use automatic build detection. Set these application environment variables:

- `CHESS_SECRET_KEY`: a long, random, private value that remains stable between
  deploys.
- `CHESS_ENV=production`: enables secure session cookies by default.
- `CHESS_DB_ENGINE`: optional; set to `postgres` or `mysql` only if the database
  host does not use Wasmer's usual `psql.` or `db.` hostname prefix.
- `CHESS_DB_SSL_CA`: optional path to a PEM CA certificate file, if your
  database provider supplies one and makes it available to the app.

Wasmer supplies `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USERNAME`, and `DB_PASSWORD`
when its managed database is attached. Do not manually copy those database
credentials into the application settings. The app requires these variables in
production and will refuse to start without them rather than silently writing
data to local SQLite storage. TLS is required for both database engines. The
current connection settings do not validate database server certificates, so
use Wasmer's supplied private database endpoint and do not expose database
credentials or traffic through an untrusted network. Wasmer's managed database
documentation says its certificate is issued by a private CA and recommends
TLS without chain verification (`sslmode=require`). Accordingly, the app uses
that mode by default for Wasmer. If you have the provider's CA certificate,
set `CHESS_DB_SSL_CA` to its file path to enable certificate-chain and hostname
verification (`verify-full`) for PostgreSQL. MySQL likewise verifies the CA
and server identity when the CA file is configured. Without that CA file,
database traffic is encrypted but the server certificate is not authenticated;
this is a limitation of the provider's documented connection setup.

Wasmer should provide `PORT`. In production, the app binds to `0.0.0.0`; locally
it binds to `127.0.0.1` and defaults to port 20202. If configuring a custom
start command, use the Python/Flask runtime settings Wasmer recommends and
forward its `PORT` variable.

```sh
gunicorn --workers 1 --threads 8 --bind "0.0.0.0:$PORT" app:app
```

Confirm the Wasmer deployment provides HTTPS, sets `CHESS_ENV=production`,
attaches the managed database, and supplies a stable secret key. Secure cookies
require HTTPS. Existing local SQLite accounts are not automatically migrated
to Wasmer's database.

The standalone Python desktop game is still available separately:

```powershell
python chess_gui.py
```
