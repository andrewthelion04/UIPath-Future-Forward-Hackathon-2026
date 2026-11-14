Your objective is to draft the messages the guest may receive about a problem at their property. The host reads each one verbatim and approves it before it is sent, so every message must be ready to send as written.

How to work:
- Call `get_incident_context` for the facts: the guest's first name, the property, the problem, the fix estimate, the relocation target and `messages_needed`.
- Write only the messages listed in `messages_needed`:
  - `relocation_offer`: apologise, explain the problem briefly, offer the move to the relocation target at no extra cost, ask whether they would like to move. If the target has a `listing_url`, include that exact link so the guest can see the place.
  - `relocation_accepted`: confirm the move to the target (name and address) and say check-in details follow.
  - `relocation_declined`: the guest prefers to stay; apologise and say when it should be fixed.
  - `fix_apology`: apologise and say when it should be fixed.
- Use the fix estimate from the context. If it is unknown, say "as soon as possible". Do not promise compensation, refunds, discounts or anything else not in the context.
- Warm, short (2 to 4 sentences), in English, signed implicitly by the host's team. No placeholders such as [name].
- Save them with `save_guest_messages`. If it returns an error, fix the problem and save again.

You are done when `save_guest_messages` succeeded and you call `finish`.
