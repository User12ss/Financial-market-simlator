// Reads one run's events (JSON, on standard input) and prints the trail the page works out from them, as JSON.
// Used by tests/test_dashboard.py to set the page's reading of a real run beside the journal's own signal table.

import { buildTrail, outcome } from "../../dashboard/static/js/decisions.js";

let text = "";
for await (const chunk of process.stdin) text += chunk;
const trail = buildTrail(JSON.parse(text));
process.stdout.write(JSON.stringify(trail.map((row) => ({ ...row, ended: outcome(row) }))));
