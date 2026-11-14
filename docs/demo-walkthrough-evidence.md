# Walkthrough — timecode to evidence

Every number spoken in the video is the number in the artefact beside it.

| In | Out | Shot | Evidence | What it shows |
| --- | --- | --- | --- | --- |
| 00:00 | 00:04 | Opening card | `docs/social-preview.png` | Title only; no claim |
| 00:04 | 00:20 | Full guard output | `.runtime/video4/card_guard.png` | Real evaluate_proposal() output: BUY 45 NVDA @ $100 |
| 00:20 | 00:30 | Clamped line close up | `same` | CLAMPED 45 -> 10.49 units, $1,049.18 = 5% of $20,983.51 |
| 00:30 | 00:52 | Three agents diagram | `README.md 'How it works in one minute'` | Roles only; no market claim |
| 00:52 | 01:07 | Rejection close up | `same guard output` | REJECTED: -$600.00 breaches -$500.00 daily loss limit |
| 01:07 | 01:21 | Offline viewer | `docs/viewer/index.html` | Synthetic fixtures, labelled on screen |
| 01:21 | 01:27 | Close | `docs/social-preview.png` | Repository URL |

## What the video does not claim

- The viewer in the replay shot plays **synthetic fixtures** and says so on screen.
- The guard output is a real `evaluate_proposal()` evaluation against the demo portfolio,
  not a recorded live trade.
- No profitability claim is made anywhere in the narration.
