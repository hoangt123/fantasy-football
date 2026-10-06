# Weekly Report

Every Tuesday a GitHub Action builds a report for your Yahoo league with:

- **Lineup check**: start/sit swaps and bye-week or injury alerts for the upcoming week.
- **Matchup preview**: both best lineups, side by side, with a win probability.
- **Waiver wire**: up to 3 add/drop moves in order (with a rough FAAB bid), plus the best available players at each position.
- **Trade ideas**: up to 5 trades that improve *both* teams' lineups, with the reasoning.
- **Roster strength**: where your starters rank in the league at each position, plus standings.

It writes Markdown, HTML and JSON files, and can email the report or publish it as a web page.

## How the recommendations work

League structure (rosters, lineup slots, scoring, standings, matchups) comes from Yahoo. Week-by-week projections for every remaining week come from Sleeper's public API.

Every candidate move gets the same test: apply it, rebuild your **best legal starting lineup for every remaining week**, and compare total starter points with your current roster. So:

- A player who would never crack your lineup is worth about 0, however good he looks.
- A fill-in for a starter's bye week is worth exactly that week's difference.
- Flex slots, injuries (Sleeper projects 0 for players ruled out) and positional scarcity are all handled the same way.

Bench depth also gets a small credit (10% of the rest-of-season projection for your top 1 QB, 2 RB, 2 WR and 1 TE backups), so the report won't cut a useful WR4 to stream a kicker.

A trade is suggested only if:

- your lineup improves,
- the other team's lineup also improves,
- and they get back at least 85% of the raw projected value they give up, so the offer doesn't look lopsided.

Scoring format (PPR, half-PPR or standard) is read from your league settings. Unusual custom scoring (6-point passing TDs, bonuses) is approximated.

## Try it now (no Yahoo access needed)

```bash
python -m weekly_report --demo          # invented 10-team league, live Sleeper projections
open reports/*/demo-league/latest.html
```

Python 3.8+ and the standard library only. Run the tests with `pytest weekly_report/tests`.

## Set up the weekly Action

Everything except Yahoo's approval can be done today. Until approval lands, scheduled runs are **skipped with a notice** (not failed), so you won't get failure emails while waiting.

1. **Yahoo app + Fantasy API application.** Create a Yahoo app as a *Web Application* (see [INSTALLATION.md](../INSTALLATION.md)), then apply at <https://sports.yahoo.com/developer/access/> with its Client ID.
2. **Run the setup wizard** (Python 3.8+, no installs needed):

   ```bash
   python -m weekly_report --setup
   ```

   It asks for your Client ID and Secret, opens Yahoo so you can log in, and saves the tokens to `.env` (git-ignored, `chmod 600`). If you set up email, it saves those settings too. With the GitHub CLI installed, it can also upload everything as repository secrets on your `origin` repo. Yahoo issues working tokens before approval, so you can do this now.

   > The redirect URI you enter must match your Yahoo app exactly. The default is `oob`, which shows a code to paste. If you registered a URL such as `https://localhost:8000/callback`, enter that instead, then paste the full address your browser lands on (the page itself may fail to load; that's fine).

3. **Check status any time:**

   ```bash
   python -m weekly_report --check
   ```

   | Result | Meaning |
   |---|---|
   | ⏳ Fantasy API approval | Tokens work; Yahoo hasn't approved the app yet. Just wait. |
   | ✅ Leagues: … | Approved; the next Tuesday run will produce a real report. |
   | ❌ … | Something needs fixing; the message says what. |

   If you'd rather add secrets by hand (Settings → Secrets and variables → Actions): `YAHOO_CLIENT_ID`, `YAHOO_CLIENT_SECRET`, `YAHOO_REFRESH_TOKEN`, and optionally `YAHOO_GUID`.

4. **Pick where the report goes** (any combination):

   | Destination | Setup |
   |---|---|
   | Files (always) | Download the `weekly-report` artifact from each run (kept 30 days). |
   | Committed to the repo | Variable `REPORT_COMMIT=true`; reports land in `reports/<season>/<league>/week-NN.*`. |
   | Email | The wizard's email step, or secrets `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `REPORT_EMAIL_TO`. For Gmail: `smtp.gmail.com`, port `587`, and an [App Password](https://myaccount.google.com/apppasswords). |
   | Web page | Variable `REPORT_PAGES=true`, plus Settings → Pages → Source: **GitHub Actions**. Set `REPORT_COMMIT=true` too, so older weeks stay in the archive. |
   | Claude "Analyst take" | Secret `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) adds a short written summary at the top. |

   Optional variable `FF_LEAGUE_KEY` (e.g. `461.l.123456`) limits the report to one league. `--check` lists your league keys once you're approved.

5. **Test delivery now**: Actions → Weekly Report → Run workflow, with **demo** ticked. Locally, `python -m weekly_report --demo --email` sends a demo report to your inbox.

> **Privacy:** this repository is public. Committed reports and the Pages site show your league's team names and rosters. Use email only, or make the repository private, if that matters to you.

## Command line

```
python -m weekly_report [--league KEY] [--week N] [--out reports] [--formats md,html,json]
                        [--email] [--site DIR] [--narrative FILE]
                        [--save-data FILE | --from-data FILE] [--demo]
python -m weekly_report --setup            # credentials wizard
python -m weekly_report --check [--ci]     # readiness: exit 0 ready, 2 waiting, 1 broken
```

`--save-data` / `--from-data` let you re-render (for example with a narrative added) without calling Yahoo again.

## Code map

| File | Role |
|---|---|
| `weekly_report/yahoo.py` | Yahoo client (token refresh, league/roster/standings/scoreboard parsing) |
| `weekly_report/sleeper.py` | Sleeper projections, season stats and trending adds, plus name matching |
| `weekly_report/lineup.py` | Optimal-lineup solver (handles flex slots) |
| `weekly_report/analysis.py` | Waivers, trades, lineup check, strengths, matchup |
| `weekly_report/render.py` | Markdown / HTML (inline styles, so it's email-safe) / JSON |
| `weekly_report/deliver.py` | Files, static site index, SMTP email |
| `weekly_report/credentials.py` | `--setup` wizard, `--check` readiness report, `.env` handling |
