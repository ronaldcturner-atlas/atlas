# Atlas light-theme shift color reference

This note preserves the approved six-color shift-status palette for the future desktop light-theme work. The mobile calendar deliberately uses a simpler two-dot system and must not replace this desktop reference.

## Desktop light-theme status palette

- My shifts: `#1f65a6`
- Available pickup: `#34d399`
- My posted shift: `#fbbf24`
- Sent trade: `#c084fc`
- Received trade: `#f472b6`
- Open shift: `#f87171`

## Mobile calendar rule

- Blue `#1f65a6`: the signed-in user is currently assigned to at least one shift on the date. This remains blue regardless of a posting or pending-trade state.
- Amber `#fbbf24`: at least one other shift on the date is open, posted for pickup, or posted for trade by another user.
- A date displays no more than one blue dot and one amber dot.
- Ordinary group shifts do not create a calendar dot.
