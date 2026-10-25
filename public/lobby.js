"use strict";

const message = document.querySelector("#message");
const accountPanel = document.querySelector("#account-panel");
const friendsPanel = document.querySelector("#friends-panel");
const accountForm = document.querySelector("#account-form");
let isRegistering = false;
let signedInUser = null;
let friendIds = new Set();
let dashboardTimer = null;
let inviteUrl = "";
let inviteTimeResolver = null;
const notifiedInvitationCodes = new Set();
const pendingInvitationPrompts = [];
let activeInvitationPrompt = null;

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

function closeInviteTimePicker(timeControl = null) {
  document.querySelector("#invite-time-modal").classList.add("hidden");
  if (inviteTimeResolver) {
    inviteTimeResolver(timeControl);
    inviteTimeResolver = null;
  }
}

function openInviteTimePicker(username) {
  const modal = document.querySelector("#invite-time-modal");
  const options = document.querySelector("#invite-time-options");
  const selectedValue = document.querySelector("#time-control").value;
  options.replaceChildren();
  for (const group of document.querySelector("#time-control").querySelectorAll("optgroup")) {
    const section = document.createElement("fieldset");
    section.className = "invite-time-group";
    const legend = document.createElement("legend");
    legend.textContent = group.label;
    section.append(legend);
    for (const option of group.querySelectorAll("option")) {
      const label = document.createElement("label");
      label.className = "invite-time-option";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "invite-time-control";
      radio.value = option.value;
      radio.checked = option.value === selectedValue;
      const description = document.createElement("span");
      description.textContent = option.textContent;
      label.append(radio, description);
      section.append(label);
    }
    options.append(section);
  }
  document.querySelector("#invite-time-opponent").textContent =
    `Choose a clock for your game with ${username}.`;
  modal.classList.remove("hidden");
  modal.querySelector('input[type="radio"]:checked')?.focus();
  return new Promise((resolve) => {
    inviteTimeResolver = resolve;
  });
}

function showNextInvitationPrompt() {
  if (activeInvitationPrompt || !pendingInvitationPrompts.length) return;
  activeInvitationPrompt = pendingInvitationPrompts.shift();
  document.querySelector("#join-prompt-message").textContent =
    `${activeInvitationPrompt.inviter} is waiting for you. Join their game when you’re ready.`;
  document.querySelector("#join-prompt-modal").classList.remove("hidden");
}

function queueInvitationPrompts(invitations) {
  for (const invite of invitations) {
    if (notifiedInvitationCodes.has(invite.room_code)) continue;
    notifiedInvitationCodes.add(invite.room_code);
    pendingInvitationPrompts.push(invite);
  }
  showNextInvitationPrompt();
}

async function joinInvitation(invite) {
  await api(`/api/rooms/${encodeURIComponent(invite.room_code)}/join`, {
    method: "POST", body: "{}",
  });
  window.location.href = `/play?room=${encodeURIComponent(invite.room_code)}`;
}

function leaderboardEntry(container, leader) {
  const row = document.createElement("div");
  row.className = "leaderboard-entry";
  const summary = document.createElement("button");
  summary.type = "button";
  summary.className = "leaderboard-summary";
  const rank = document.createElement("span");
  rank.className = "leaderboard-rank";
  rank.textContent = String(leader.rank).padStart(2, "0");
  const identity = document.createElement("span");
  identity.className = "leaderboard-identity";
  const name = document.createElement("strong");
  name.textContent = leader.username;
  const hint = document.createElement("small");
  hint.textContent = "View player options";
  identity.append(name, hint);
  const rating = document.createElement("strong");
  rating.className = "leaderboard-rating";
  rating.textContent = `${leader.rating} ELO`;
  summary.append(rank, identity, rating);

  const actions = document.createElement("div");
  actions.className = "leaderboard-actions hidden";
  const profileButton = button("View profile", () => {
    openPlayerProfile(leader.id).catch((error) => tell(error.message));
  });
  const friendButton = button("Send friend request", async () => {
    try {
      await api("/api/friends/requests", {
        method: "POST",
        body: JSON.stringify({ username: leader.username }),
      });
      tell(`Friend request sent to ${leader.username}.`);
      await loadDashboard();
    } catch (error) {
      tell(error.message);
    }
  }, true);
  if (friendIds.has(Number(leader.id))) {
    friendButton.textContent = "Friends";
    friendButton.disabled = true;
    friendButton.title = "You are already friends.";
  } else if (signedInUser?.id === leader.id) {
    friendButton.textContent = "Your account";
    friendButton.disabled = true;
    friendButton.title = "This is your account.";
  }
  actions.append(profileButton, friendButton);
  summary.addEventListener("click", () => {
    const isOpen = !actions.classList.contains("hidden");
    for (const menu of container.querySelectorAll(".leaderboard-actions")) {
      menu.classList.add("hidden");
    }
    actions.classList.toggle("hidden", isOpen);
    summary.setAttribute("aria-expanded", String(!isOpen));
  });
  summary.setAttribute("aria-expanded", "false");
  row.append(summary, actions);
  container.append(row);
}

async function loadLeaderboard() {
  const container = document.querySelector("#leaderboard-list");
  try {
    const data = await api("/api/leaderboard");
    container.replaceChildren();
    if (!data.leaders.length) {
      container.innerHTML = '<p class="muted">No accounts yet. Create an account to claim the first spot.</p>';
      return;
    }
    data.leaders.forEach((leader, index) => {
      leaderboardEntry(container, { ...leader, rank: index + 1 });
    });
  } catch (error) {
    container.replaceChildren();
    const status = document.createElement("p");
    status.className = "muted";
    status.textContent = error.message;
    container.append(status);
  }
}

function formatProfileDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "Date unavailable"
    : new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(date);
}

async function openPlayerProfile(userId) {
  const { profile } = await api(`/api/users/${encodeURIComponent(userId)}/profile`);
  document.querySelector("#profile-name").textContent = profile.username;
  document.querySelector("#profile-member").textContent =
    `Member since ${formatProfileDate(profile.memberSince)}`;
  document.querySelector("#profile-rating").textContent = profile.rating;
  document.querySelector("#profile-wins").textContent = profile.record.wins;
  document.querySelector("#profile-draws").textContent = profile.record.draws;
  document.querySelector("#profile-losses").textContent = profile.record.losses;

  const games = document.querySelector("#profile-games");
  games.replaceChildren();
  if (!profile.recentGames.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No completed matches yet.";
    games.append(empty);
  }
  for (const game of profile.recentGames) {
    const row = document.createElement("div");
    row.className = `profile-game ${game.outcome}`;
    const outcome = document.createElement("strong");
    outcome.className = "profile-outcome";
    outcome.textContent = game.outcome.toUpperCase();
    const detail = document.createElement("span");
    detail.className = "profile-game-detail";
    const opponent = document.createElement("strong");
    opponent.textContent = `vs ${game.opponent}`;
    const result = document.createElement("small");
    result.textContent = `${game.result} · ${formatProfileDate(game.playedAt)}`;
    detail.append(opponent, result);
    const change = document.createElement("span");
    change.className = "profile-rating-change";
    if (game.ratingChange === null) {
      change.textContent = "—";
    } else {
      change.textContent = `${game.ratingChange >= 0 ? "+" : ""}${game.ratingChange}`;
      change.classList.add(game.ratingChange >= 0 ? "positive" : "negative");
    }
    row.append(outcome, detail, change);
    games.append(row);
  }
  document.querySelector("#profile-modal").classList.remove("hidden");
}

function setAccountView(user) {
  signedInUser = user;
  friendIds = new Set();
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
  loadLeaderboard();
}

async function loadDashboard() {
  if (!signedInUser) return;
  try {
    const data = await api("/api/dashboard");
    friendIds = new Set(data.friends.map((friend) => Number(friend.id)));
    void loadLeaderboard();
    const friends = document.querySelector("#friend-list");
    friends.replaceChildren();
    if (!data.friends.length) friends.innerHTML = '<p class="muted">No friends yet.</p>';
    for (const friend of data.friends) {
      item(friends, `${friend.username} · ${friend.rating} ELO`, "Invite", async () => {
        try {
          const timeControl = await openInviteTimePicker(friend.username);
          if (!timeControl) return;
          const invite = await api("/api/invitations", {
            method: "POST",
            body: JSON.stringify({
              friendId: friend.id,
              timeControl,
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
    queueInvitationPrompts(data.invitations);
    for (const invite of data.invitations) {
      item(invitations, `${invite.inviter} is ready to play`, "Join game", async () => {
        try {
          await joinInvitation(invite);
        } catch (error) { tell(error.message); }
      }, true);
    }

    const games = document.querySelector("#game-list");
    games.replaceChildren();
    if (!data.games.length) games.innerHTML = '<p class="muted">No active games.</p>';
    for (const game of data.games) {
      const white = game.white_name || "Guest";
      const black = game.black_name || (game.status === "waiting" ? "Waiting for player" : "Guest");
      const row = document.createElement("div");
      row.className = "game-list-item";
      const details = document.createElement("div");
      details.className = "game-list-details";
      const players = document.createElement("strong");
      players.textContent = `${white} vs ${black}`;
      const status = document.createElement("span");
      status.textContent = game.status === "waiting"
        ? "Your room is ready when your opponent joins."
        : "Your match is in progress. Pick up where you left off.";
      details.append(players, status);
      const continueButton = button(
        game.status === "waiting" ? "View room" : "Rejoin game",
        () => {
          window.location.href = `/play?room=${encodeURIComponent(game.room_code)}`;
        },
        true,
      );
      const closeButton = button("Close room", async () => {
        const confirmation = game.status === "active"
          ? "Close this active game? This will forfeit the match and may change both players’ ratings."
          : "Close this waiting room? Your opponent will no longer be able to join.";
        if (!window.confirm(confirmation)) return;
        closeButton.disabled = true;
        try {
          await api(`/api/rooms/${encodeURIComponent(game.room_code)}/close`, {
            method: "POST",
            body: "{}",
          });
          tell(game.status === "active"
            ? "Game closed and recorded as a forfeit."
            : "Waiting room closed.");
          await loadDashboard();
        } catch (error) {
          tell(error.message);
          closeButton.disabled = false;
        }
      });
      closeButton.classList.add("danger");
      row.append(details, continueButton, closeButton);
      games.append(row);
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

function closeProfile() {
  document.querySelector("#profile-modal").classList.add("hidden");
}

document.querySelector("#close-profile").addEventListener("click", closeProfile);
document.querySelector("#profile-modal").addEventListener("click", (event) => {
  if (event.target.id === "profile-modal") closeProfile();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeProfile();
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

document.querySelector("#invite-time-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const selected = document.querySelector('input[name="invite-time-control"]:checked');
  if (!selected) {
    tell("Choose a time control before sending the invitation.");
    return;
  }
  closeInviteTimePicker(selected.value);
});
document.querySelector("#cancel-invite-time").addEventListener("click", () => {
  closeInviteTimePicker();
});
document.querySelector("#close-invite-time").addEventListener("click", () => {
  closeInviteTimePicker();
});
document.querySelector("#invite-time-modal").addEventListener("click", (event) => {
  if (event.target.id === "invite-time-modal") closeInviteTimePicker();
});
document.querySelector("#join-prompt-join").addEventListener("click", async () => {
  if (!activeInvitationPrompt) return;
  const invite = activeInvitationPrompt;
  try {
    await joinInvitation(invite);
  } catch (error) {
    tell(error.message);
    document.querySelector("#join-prompt-modal").classList.add("hidden");
    activeInvitationPrompt = null;
    showNextInvitationPrompt();
  }
});
document.querySelector("#join-prompt-later").addEventListener("click", () => {
  document.querySelector("#join-prompt-modal").classList.add("hidden");
  activeInvitationPrompt = null;
  showNextInvitationPrompt();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" &&
      !document.querySelector("#invite-time-modal").classList.contains("hidden")) {
    closeInviteTimePicker();
  }
  if (event.key === "Escape" &&
      !document.querySelector("#join-prompt-modal").classList.contains("hidden")) {
    document.querySelector("#join-prompt-later").click();
  }
});

api("/api/session")
  .then(({ user }) => setAccountView(user))
  .catch((error) => tell(error.message));
