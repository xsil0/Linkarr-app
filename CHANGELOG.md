# Changelog

## 1.1.1

### Fixed

- **Long posts lost most of their links.** Only the first 20 Base64 codes in a pasted post were decoded,
  and at most 40 links were read, so a thread with 100+ links came back with about 30. Now every code is
  decoded and up to 250 links are read from one paste (up to 400,000 characters, enough for a whole
  forum page).

## 1.1.0

### New

- **Title cards.** A show or movie without a poster gets a card in a muted colour picked from its name
  (the same show always gets the same colour), with the name on it, until the poster arrives. In Coming
  up, History and everywhere else a poster shows.
- **Posters for tracked shows.** Tracking a show saves its poster right away. Shows tracked before this
  update get theirs over the first few hours.
- **Library is a list you filter as you type.** Every folder in every library, with how many seasons
  are on disk, whether it's tracked, and what's missing. Tap one for its seasons and episode counts.
  **Search TMDB** is still there for what isn't in your library.
- **Track all my series:** a folder it can't match says what the episode files inside call the show,
  and Find searches for that. **Also rename the folder to the show's name** (off unless you tick it).
- **Your download site's logo:** upload a small picture in Settings → Accounts to show instead of the
  icon letters.
- **Back to the same thread for its new links:** the link picker leaves unticked what you already have,
  either links you downloaded before or titles (and seasons) already in a library folder, and says so.
  Tick one to get it again.
- Posters on TMDB search results in Library.

### Changed

- **Free space check** counts every download that's running, leaves room to unpack archives (about
  double their size), and checks the library drive too when it's a different disk. The alert says
  which drive is short.
- **Sign-in works over plain http** on your home network with nothing to set. Over HTTPS the cookie is
  still HTTPS-only. `LA_COOKIE_SECURE=1` makes it HTTPS-only always, `0` never.
- The first-run page says step by step where to find the setup code.
- The paste bar is off Library and History, which have their own search.
- Show dates read the same everywhere ("Sun, Nov 15"), and show status is in plain words ("Airing"
  instead of TMDB's "Returning Series").
- Alert titles say what went wrong ("This video won't play", not "A download got stuck").
- The menu no longer says "MEGA not signed in" under the MEGA allowance meters.

## 1.0.2

- MEGA storage (used of total) shows under the transfer allowance in the menu, and in
  **Check the allowance now**.
- Long show descriptions in search results wrap to two lines instead of running off the screen, so
  **Track this** and **Use this** stay in view.

## 1.0.1

- Track all my series: folders it can't match now have **Find** and **Track this** right in the list.
  Before, Find opened Library search, which had no way to track the folder.
- Track all matches more folders: a year at the end without brackets ("Gannibal 2022") and a note
  in brackets ("Family Plan (Korean)") no longer stop the match.
- The menu shows the MEGA account right after a restart, not only after opening Setup check.
- **Check the allowance now** reads the session MEGAcmd saves correctly, and says what went wrong
  instead of "Sign in first" when you are signed in.

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
