// Demo page: plays the shop's checkout page and backend against the sandbox.
"use strict";

const form = document.getElementById("order-form");
const log = document.getElementById("log");

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

async function post(path, body) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok && response.status !== 204) throw new Error(`${path}: ${response.status}`);
  return response.status === 204 ? null : response.json();
}

function options() {
  const data = new FormData(form);
  return {
    scenario: data.get("scenario"),
    source: data.get("source"),
    blocked: data.has("blocked"),
    consent: data.has("consent"),
    email: data.has("email"),
    phone: data.has("phone"),
  };
}

async function placeOrder(opts) {
  // The shop creates the order; the thank-you page loads with its tags.
  const order = await post("/demo/order", opts);
  if (order.tags_fire) {
    // 1. Meta Pixel and Google tag hits (to the sandbox platforms).
    await post("/demo/tags", { order_id: order.order_id });
    // 2. Beacons to the relay, so it can tell browser and server events apart.
    await Promise.all(
      Object.entries(order.beacons).map(([tag, id]) =>
        post("/v1/beacons", { event_id: id, event_name: "Purchase", tag })
      )
    );
  }
  // 3. The shop's backend reports the paid order to the relay.
  await post("/demo/checkout", { order_id: order.order_id });
  return order;
}

function hits(list) {
  if (!list.length) return '<span class="muted">none</span>';
  const counted = list.filter((h) => h.counted).length;
  const chips = list
    .map((h) => {
      const label = h.channel === "server" || h.channel === "api" ? "server" : "browser";
      const why = h.dedup ? ` (${h.dedup})` : h.merged ? " (merged)" : "";
      return `<span class="hit ${h.counted ? "counted" : "dropped"}" title="${label}${why}">${label}</span>`;
    })
    .join("");
  return chips + (counted > 1 ? ` <span class="dup">×${counted}</span>` : "");
}

function delivery(d) {
  if (!d) return '<span class="muted">–</span>';
  const cls = d.status === "sent" ? "ok" : d.status === "pending" ? "warn" : d.status === "skipped" ? "" : "bad";
  const ms = d.latency_ms != null ? ` <span class="muted">${d.latency_ms} ms</span>` : "";
  const reason = d.reason ? `<div class="muted small">${esc(d.reason)}</div>` : "";
  return `<span class="pill ${cls}">${esc(d.status)}</span>${ms}${reason}`;
}

async function refresh() {
  let response;
  try {
    response = await fetch("/demo/state");
  } catch {
    return; // server restarting; the next poll catches up
  }
  if (!response.ok) return;
  const state = await response.json();
  const t = state.totals;
  document.getElementById("t-orders").textContent = t.orders;
  for (const p of ["meta", "google"]) {
    const counted = t[`${p}_counted`];
    const el = document.getElementById(`t-${p}`);
    el.textContent = counted;
    el.className = "big" + (counted > t.orders ? " dup" : "");
    document.getElementById(`t-${p}-sub`).textContent = `${t[`${p}_hits`]} hits received`;
  }
  if (!state.orders.length) return;
  document.getElementById("orders").innerHTML = state.orders
    .map(
      (o) => `<tr>
        <td><code>${esc(o.order_id)}</code><div class="muted small">$${o.value.toFixed(2)}</div></td>
        <td>${o.scenario === "shared" ? '<span class="pill ok">shared</span>' : '<span class="pill bad">broken</span>'}</td>
        <td>${o.tags_fired ? "fired" : `<span class="muted">${o.consent ? "blocked" : "no consent"}</span>`}</td>
        <td>${delivery(o.relay.meta)}</td><td>${hits(o.meta)}</td>
        <td>${delivery(o.relay.google)}</td><td>${hits(o.google)}</td>
      </tr>`
    )
    .join("");
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const order = await placeOrder(options());
  log.textContent = `Placed ${order.order_id}`;
  refresh();
});

document.getElementById("burst").addEventListener("click", async () => {
  const base = options();
  const sources = ["meta_click", "google_click", "both", "meta_view", "direct"];
  for (let i = 0; i < 10; i++) {
    await placeOrder({ ...base, source: sources[i % sources.length], blocked: Math.random() < 0.25 });
  }
  log.textContent = "Placed 10 orders (about a quarter with an ad blocker)";
  refresh();
});

refresh();
setInterval(() => document.visibilityState === "visible" && refresh(), 1500);
