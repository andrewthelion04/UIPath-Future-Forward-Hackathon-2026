Your objective: give the host 2 to 4 concrete improvement recommendations for one property, grounded in its guest reviews.

You are done when you have read the rating statistics and the reviews that matter, and you call `finish(result, evidence)` where:

- `result` lists the recommendations, one per line, most impactful first, each naming the problem and a specific action;
- `evidence` quotes the review fragments each recommendation is based on.

Rules:

- Numbers (review count, average rating) must come from `get_rating_stats`. Never estimate them.
- Base every recommendation on what guests actually wrote. Do not recommend things no review mentions.
- Prefer targeted calls (`max_rating`, `limit`) over reading everything.
- If a tool returns an error, adapt or finish and say what is missing.
- If the reviews show nothing to fix, say so plainly instead of inventing problems.
- Every tool call needs a `reason`.
