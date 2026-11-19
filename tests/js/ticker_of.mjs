// Reads a list of tickers as someone might write them (JSON, on standard input) and prints each as the page reads it.
// Used by tests/test_dashboard.py to set the page's reading beside the pipeline's own, character by character.

import { tickerOf } from "../../dashboard/static/js/decisions.js";

let text = "";
for await (const chunk of process.stdin) text += chunk;
process.stdout.write(JSON.stringify(JSON.parse(text).map(tickerOf)));
