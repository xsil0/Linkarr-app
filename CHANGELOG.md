# Changelog

## 1.0.0

First public release.

### New

- **Link picker.** Paste a whole post (or send it with Send to Linkarr) and pick which links to add.
  One quality per title is ticked, every season is ticked, captcha links are held. Nothing is added
  until you press Add.
- **Send to Linkarr.** A bookmark (Settings → Accounts) that sends the links on the page you're
  reading to your Linkarr. Android's Share menu does the same once Linkarr is installed.
- **Saved archive passwords** with `{year}`, `{title}`, `{Title}` and `{title_nospace}`.
- **Title not recognised:** search TMDB from the job and rename it before it's filed.
- **Jellyfin and Emby** library scans, with a Test button.
- **Password reset:** `docker exec linkarr python -m linkarr reset-password`.
- New look: one dark and one light theme, IBM Plex type, a Now Downloading banner, small posters for
  what's coming up in the Tracker.

### Changed

- Settings is now eight pages (Setup check, Libraries, Accounts, Media servers, Telegram, Downloads,
  Naming, System) with one Save bar. On a phone the pages are a drop-down.
- Activity puts everything that needs you in one list at the top, failed downloads included. A failed
  download no longer sits under Downloading.
- Nightly chat clear is off until you turn it on.
- No captcha sites are built in: add the ones you use in Settings → Downloads.
- The old themes (Poster, Poster Mono, Mono, Terminal) are gone; they show as Dark.
- Saved archive passwords are only in the backup that includes keys.

### Security

- Sign-in and the setup code lock after 5 wrong tries from one address and 20 overall.
- `LA_TRUSTED_PROXIES` sets whose `X-Forwarded-For` is believed.
