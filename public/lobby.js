"use strict";

const message = document.querySelector("#message");
const accountPanel = document.querySelector("#account-panel");
const friendsPanel = document.querySelector("#friends-panel");
const accountForm = document.querySelector("#account-form");
let isRegistering = false;
let signedInUser = null;
let dashboardTimer = null;
let inviteUrl = "";

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The request failed.");
  return result;
}

function tell(text) {
  message.textContent = text;
}

function button(text, action, primary = false) {
  const element = document.createElement("button");
  element.type = "button";
  element.className = `button${primary ? " primary" : ""}`;
  element.textContent = text;
  element.addEventListener("click", action);
  return element;
}

function item(container, label, actionLabel, action, primary = false) {
  const row = document.createElement("div");
  row.className = "list-item";
  const name = document.createElement("span");
  name.textContent = label;
  row.append(name, button(actionLabel, action, primary));
  container.append(row);
}

function setAccountView(user) {
  signedInUser = user;
  const signedIn = Boolean(user);
  accountPanel.classList.toggle("hidden", signedIn);
  friendsPanel.classList.toggle("hidden", !signedIn);
  document.querySelector("#logout-button").classList.toggle("hidden", !signedIn);
  if (signedIn) {
    document.querySelector("#welcome-name").textContent = `Hello, ${user.username}`;
    loadDashboard();
    if (dashboardTimer) clearInterval(dashboardTimer);
    dashboardTimer = setInterval(loadDashboard, 5000);
  } else if (dashboardTimer) {
    clearInterval(dashboardTimer);
    dashboardTimer = null;
  }
}

async function loadDashboard() {
  if (!signedInUser) return;
  try {
    const data = await api("/api/dashboard");
    const friends = document.querySelector("#friend-list");
    friends.replaceChildren();
    if (!data.friends.length) friends.innerHTML = '<p class="muted">No friends yet.</p>';
    for (const friend of data.friends) {
      item(friends, `${friend.username} · ${friend.rating} ELO`, "Invite", async () => {
        try {
          const invite = await api("/api/invitations", {
            method: "POST",
            body: JSON.stringify({
              friendId: friend.id,
              timeControl: document.querySelector("#time-control").value,
            }),
          });
          tell(`Invitation sent to ${friend.username}.`);
          await copyInvite(invite.roomCode);
          await loadDashboard();
        } catch (error) { tell(error.message); }
      }, true);
    }

    const requests = document.querySelector("#request-list");
    requests.replaceChildren();
    if (!data.requests.length) requests.innerHTML = '<p class="muted">No new requests.</p>';
    for (const request of data.requests) {
      item(requests, request.username, "Accept", async () => {
        try {
          await api(`/api/friends/requests/${request.id}`, {
            method: "POST", body: JSON.stringify({ accept: true }),
          });
          tell(`You and ${request.username} are now friends.`);
          await loadDashboard();
        } catch (error) { tell(error.message); }
      }, true);
      const row = requests.lastElementChild;
      row.append(button("Decline", async () => {
        try {
          await api(`/api/friends/requests/${request.id}`, {
            method: "POST", body: JSON.stringify({ accept: false }),
          });
          await loadDashboard();
        } catch (error) { tell(error.message); }
      }));
    }

    const invitations = document.querySelector("#invitation-list");
    invitations.replaceChildren();
    if (!data.invitations.length) invitations.innerHTML = '<p class="muted">No invitations waiting.</p>';
    for (const invite of data.invitations) {
      item(invitations, `${invite.inviter} invited you`, "Accept", async () => {
        try {
          await api(`/api/rooms/${encodeURIComponent(invite.room_code)}/join`, {
            method: "POST", body: "{}",
          });
          window.location.href = `/play?room=${encodeURIComponent(invite.room_code)}`;
        } catch (error) { tell(error.message); }
      }, true);
    }

    const games = document.querySelector("#game-list");
    games.replaceChildren();
    if (!data.games.length) games.innerHTML = '<p class="muted">No active games.</p>';
    for (const game of data.games) {
      const white = game.white_name || "Guest";
      const black = game.black_name || (game.status === "waiting" ? "Waiting for player" : "Guest");
      item(games, `${white} vs ${black}`, "Open", () => {
        window.location.href = `/play?room=${encodeURIComponent(game.room_code)}`;
      });
    }
  } catch (error) {
    tell(error.message);
  }
}

async function copyInvite(roomCode) {
  inviteUrl = new URL(`/play?room=${encodeURIComponent(roomCode)}`, window.location.origin).href;
  const invitePanel = document.querySelector("#invite-share");
  const inviteInput = document.querySelector("#invite-url");
  inviteInput.value = inviteUrl;
  invitePanel.classList.remove("hidden");
  try {
    await navigator.clipboard.writeText(inviteUrl);
    tell("Invite link copied. Paste it anywhere to invite your opponent.");
  } catch {
    tell("Select the invite link below and copy it to share with your opponent.");
    inviteInput.focus();
    inviteInput.select();
  }
}

document.querySelector("#copy-invite").addEventListener("click", async () => {
  if (!inviteUrl) return;
  try {
    await navigator.clipboard.writeText(inviteUrl);
    tell("Invite link copied.");
  } catch {
    const input = document.querySelector("#invite-url");
    input.focus();
    input.select();
    tell("The link is selected. Copy it to share with your opponent.");
  }
});

document.querySelector("#account-switch").addEventListener("click", () => {
  isRegistering = !isRegistering;
  document.querySelector("#account-heading").textContent = isRegistering ? "Create your account" : "Welcome back";
  document.querySelector("#account-submit").textContent = isRegistering ? "Create account" : "Sign in";
  document.querySelector("#account-switch").textContent = isRegistering ? "Already have an account? Sign in" : "Create an account";
  document.querySelector("#password-input").autocomplete = isRegistering ? "new-password" : "current-password";
});

accountForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const buttonElement = document.querySelector("#account-submit");
  buttonElement.disabled = true;
  try {
    const path = isRegistering ? "/api/register" : "/api/login";
    const result = await api(path, {
      method: "POST",
      body: JSON.stringify({
        username: document.querySelector("#username-input").value.trim(),
        password: document.querySelector("#password-input").value,
      }),
    });
    document.querySelector("#password-input").value = "";
    setAccountView(result.user);
    tell(`Signed in as ${result.user.username}.`);
  } catch (error) {
    tell(error.message);
  } finally {
    buttonElement.disabled = false;
  }
});

document.querySelector("#logout-button").addEventListener("click", async () => {
  try {
    await api("/api/logout", { method: "POST", body: "{}" });
    setAccountView(null);
    tell("You have signed out.");
  } catch (error) { tell(error.message); }
});

document.querySelector("#create-room").addEventListener("click", async () => {
  try {
    const room = await api("/api/rooms", {
      method: "POST",
      body: JSON.stringify({ timeControl: document.querySelector("#time-control").value }),
    });
    await copyInvite(room.roomCode);
    window.location.href = room.url;
  } catch (error) { tell(error.message); }
});

document.querySelector("#join-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const code = document.querySelector("#room-code").value.trim().toUpperCase();
  try {
    await api(`/api/rooms/${encodeURIComponent(code)}/join`, { method: "POST", body: "{}" });
    window.location.href = `/play?room=${encodeURIComponent(code)}`;
  } catch (error) { tell(error.message); }
});

document.querySelector("#friend-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const result = await api("/api/friends/requests", {
      method: "POST",
      body: JSON.stringify({ username: document.querySelector("#friend-username").value.trim() }),
    });
    document.querySelector("#friend-username").value = "";
    tell(result.status === "friends" ? "Friend request accepted." : "Friend request sent.");
    await loadDashboard();
  } catch (error) { tell(error.message); }
});

api("/api/session")
  .then(({ user }) => setAccountView(user))
  .catch((error) => tell(error.message));
