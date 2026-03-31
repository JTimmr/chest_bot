/**
 * Chest Bot API - JavaScript Example
 * 
 * Works in browser or Node.js (with fetch).
 * Replace YOUR_API_KEY with the actual key.
 * 
 * NOTE: If calling from browser JavaScript, the API key will be
 * visible to users in their browser's dev tools. For production,
 * it's better to call the API from the PHP backend instead.
 */

const API_URL = "https://fbctoapi.xyz";
const API_KEY = "YOUR_API_KEY";

async function chestApiGet(endpoint) {
  const response = await fetch(`${API_URL}${endpoint}`, {
    headers: { "X-API-Key": API_KEY },
  });
  if (!response.ok) {
    throw new Error(`API error: ${response.status} ${response.statusText}`);
  }
  return response.json();
}

// --- Get leaderboard ---
async function getLeaderboard() {
  return chestApiGet("/api/v1/leaderboard");
}

// --- Get stats ---
async function getStats() {
  return chestApiGet("/api/v1/stats");
}

// --- Get recent transactions ---
async function getRecent(limit = 10) {
  return chestApiGet(`/api/v1/recent?limit=${limit}`);
}


// ============================================================
// EXAMPLE: Render leaderboard into a DOM element
// ============================================================
async function renderLeaderboard(elementId) {
  const container = document.getElementById(elementId);
  if (!container) return;

  try {
    const data = await getLeaderboard();

    let html = `<h3>Donation Leaderboard — $${data.total_raised_usd.toFixed(2)} raised</h3>`;
    html += `<table><thead><tr><th>#</th><th>Donor</th><th>Amount (USD)</th></tr></thead><tbody>`;

    for (const entry of data.entries) {
      html += `<tr>
        <td>${entry.rank}</td>
        <td>${entry.display_name}</td>
        <td>$${entry.donated_usd.toFixed(2)}</td>
      </tr>`;
    }

    html += `</tbody></table>`;
    container.innerHTML = html;
  } catch (err) {
    container.innerHTML = `<p>Unable to load leaderboard: ${err.message}</p>`;
  }
}

// ============================================================
// EXAMPLE: Render recent transactions into a DOM element
// ============================================================
async function renderRecent(elementId, limit = 10) {
  const container = document.getElementById(elementId);
  if (!container) return;

  try {
    const data = await getRecent(limit);

    let html = `<h3>Recent Donations</h3>`;
    html += `<table><thead><tr><th>Time</th><th>Amount</th><th>Token</th><th>Value (USD)</th><th>From</th></tr></thead><tbody>`;

    for (const tx of data.transactions) {
      const time = new Date(tx.timestamp).toLocaleString();
      html += `<tr>
        <td>${time}</td>
        <td>${tx.amount_ui}</td>
        <td>${tx.token}</td>
        <td>$${tx.value_usdc.toFixed(2)}</td>
        <td>${tx.sender_wallet}</td>
      </tr>`;
    }

    html += `</tbody></table>`;
    container.innerHTML = html;
  } catch (err) {
    container.innerHTML = `<p>Unable to load transactions: ${err.message}</p>`;
  }
}


// ============================================================
// Usage in HTML:
//
//   <div id="leaderboard"></div>
//   <div id="recent"></div>
//   <script src="fetch_data.js"></script>
//   <script>
//     renderLeaderboard("leaderboard");
//     renderRecent("recent", 10);
//   </script>
// ============================================================
