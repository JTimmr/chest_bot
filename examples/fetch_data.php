<?php
/**
 * Chest Bot API - PHP Example for WordPress
 * 
 * Drop this in your theme's functions.php or use it in a custom plugin/shortcode.
 * Replace YOUR_API_KEY with the actual key.
 */

define('CHEST_API_URL', 'https://fbctoapi.xyz');
define('CHEST_API_KEY', 'YOUR_API_KEY');

function chest_api_get($endpoint) {
    $url = CHEST_API_URL . $endpoint;
    
    $response = wp_remote_get($url, [
        'headers' => [
            'X-API-Key' => CHEST_API_KEY,
        ],
        'timeout' => 10,
    ]);

    if (is_wp_error($response)) {
        return null;
    }

    $body = wp_remote_retrieve_body($response);
    return json_decode($body, true);
}

// --- Get leaderboard ---
function chest_get_leaderboard() {
    return chest_api_get('/api/v1/leaderboard');
}

// --- Get stats (total raised, targets, token breakdown) ---
function chest_get_stats() {
    return chest_api_get('/api/v1/stats');
}

// --- Get recent transactions ---
function chest_get_recent($limit = 10) {
    return chest_api_get('/api/v1/recent?limit=' . intval($limit));
}


// ============================================================
// EXAMPLE: Shortcode to display leaderboard [chest_leaderboard]
// ============================================================
function chest_leaderboard_shortcode() {
    $data = chest_get_leaderboard();
    if (!$data) return '<p>Unable to load leaderboard.</p>';

    $html = '<div class="chest-leaderboard">';
    $html .= '<h3>Donation Leaderboard — $' . number_format($data['total_raised_usd'], 2) . ' raised</h3>';
    $html .= '<table><thead><tr><th>#</th><th>Donor</th><th>Amount (USD)</th></tr></thead><tbody>';

    foreach ($data['entries'] as $entry) {
        $html .= '<tr>';
        $html .= '<td>' . $entry['rank'] . '</td>';
        $html .= '<td>' . esc_html($entry['display_name']) . '</td>';
        $html .= '<td>$' . number_format($entry['donated_usd'], 2) . '</td>';
        $html .= '</tr>';
    }

    $html .= '</tbody></table></div>';
    return $html;
}
add_shortcode('chest_leaderboard', 'chest_leaderboard_shortcode');


// ============================================================
// EXAMPLE: Shortcode to display recent transactions [chest_recent]
// ============================================================
function chest_recent_shortcode($atts) {
    $atts = shortcode_atts(['limit' => 10], $atts);
    $data = chest_get_recent($atts['limit']);
    if (!$data) return '<p>Unable to load transactions.</p>';

    $html = '<div class="chest-recent">';
    $html .= '<h3>Recent Donations</h3>';
    $html .= '<table><thead><tr><th>Time</th><th>Amount</th><th>Token</th><th>Value (USD)</th><th>From</th></tr></thead><tbody>';

    foreach ($data['transactions'] as $tx) {
        $time = date('M j, H:i', strtotime($tx['timestamp']));
        $html .= '<tr>';
        $html .= '<td>' . $time . '</td>';
        $html .= '<td>' . $tx['amount_ui'] . '</td>';
        $html .= '<td>' . esc_html($tx['token']) . '</td>';
        $html .= '<td>$' . number_format($tx['value_usdc'], 2) . '</td>';
        $html .= '<td>' . esc_html($tx['sender_wallet']) . '</td>';
        $html .= '</tr>';
    }

    $html .= '</tbody></table></div>';
    return $html;
}
add_shortcode('chest_recent', 'chest_recent_shortcode');


// ============================================================
// EXAMPLE: Shortcode to display stats & targets [chest_stats]
// ============================================================
function chest_stats_shortcode() {
    $data = chest_get_stats();
    if (!$data) return '<p>Unable to load stats.</p>';

    $html = '<div class="chest-stats">';

    // Total raised
    $html .= '<h3>Total Raised: $' . number_format($data['total_raised_usd'], 2) . '</h3>';

    // Breakdown by token
    $html .= '<div class="chest-token-breakdown">';
    $html .= '<h4>By Token</h4>';
    $html .= '<ul>';
    foreach ($data['raised_by_token'] as $token => $amount) {
        $html .= '<li><strong>' . esc_html($token) . ':</strong> $' . number_format($amount, 2) . '</li>';
    }
    $html .= '</ul></div>';

    // Targets / milestones
    if (!empty($data['targets'])) {
        $html .= '<div class="chest-targets">';
        $html .= '<h4>Targets</h4>';
        foreach ($data['targets'] as $target) {
            $pct = $target['progress_percent'];
            $status = $target['completed'] ? '✅ Completed' : number_format($pct, 1) . '%';
            $html .= '<div class="chest-target">';
            $html .= '<p><strong>' . esc_html($target['target_name']) . '</strong>';
            $html .= ' — $' . number_format($target['target_amount'], 0) . ' (' . $status . ')</p>';

            // Progress bar
            if (!$target['completed']) {
                $html .= '<div style="background:#e0e0e0;border-radius:8px;height:20px;width:100%;overflow:hidden;">';
                $html .= '<div style="background:#4ea8de;height:100%;width:' . min(100, $pct) . '%;border-radius:8px;"></div>';
                $html .= '</div>';
            }

            $html .= '</div>';
        }
        $html .= '</div>';
    }

    // Next target highlight
    if (!empty($data['next_target'])) {
        $next = $data['next_target'];
        $html .= '<div class="chest-next-target">';
        $html .= '<p><strong>Next milestone:</strong> ' . esc_html($next['target_name']);
        $html .= ' — $' . number_format($data['total_raised_usd'], 2);
        $html .= ' / $' . number_format($next['target_amount'], 0);
        $html .= ' (' . number_format($next['progress_percent'], 1) . '%)</p>';
        $html .= '</div>';
    }

    $html .= '</div>';
    return $html;
}
add_shortcode('chest_stats', 'chest_stats_shortcode');


// ============================================================
// AVAILABLE SHORTCODES:
//   [chest_leaderboard]       — Full donor leaderboard
//   [chest_recent limit=10]   — Recent transactions (default 10)
//   [chest_stats]             — Total raised, token breakdown,
//                                targets with progress bars
//
// AVAILABLE DATA FIELDS PER ENDPOINT:
//
// /api/v1/leaderboard:
//   total_raised_usd, entries[].rank, entries[].display_name,
//   entries[].donated_usd, entries[].is_anonymous, total_entries
//
// /api/v1/stats:
//   total_raised_usd,
//   raised_by_token.USDC, raised_by_token.USDT,
//   raised_by_token.FARTBOY, raised_by_token.SOL,
//   targets[].id, targets[].target_amount, targets[].target_name,
//   targets[].completed, targets[].completed_at, targets[].progress_percent,
//   next_target (same fields as above, or null)
//
// /api/v1/recent:
//   transactions[].timestamp, transactions[].amount_ui,
//   transactions[].token, transactions[].value_usdc,
//   transactions[].sender_wallet, total_count
// ============================================================
?>
