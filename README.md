# Linkarr

Self-hosted organizer that turns file-host share links into a clean, Plex-ready library. Paste a link,
and Linkarr downloads, unpacks, names and sorts the files into the right folders. Docker-ready, runs
on your own server, no cloud needed.

![Linkarr screenshot](docs/screenshot.png)
<!-- Screenshot to come: the Activity page with a few finished jobs. -->

## What it is

Linkarr is one Docker container with a web app (it installs on your phone too) and an optional
Telegram bot. You give it a MEGA link. It signs in with **your own** MEGA account, reads what's inside
before downloading, looks the title up on TMDB, and asks you to confirm anything it isn't sure about.
Then it downloads, unpacks, names everything the way Plex expects
(`Movie Name (Year)/Movie Name (Year).mkv`, `Show (Year)/Season 2/Show - S02E01.mkv`), moves it into
the right library folder and tells Plex, Jellyfin or Emby to scan.

## How it fits next to Sonarr and Radarr

Sonarr and Radarr search indexers and grab releases for you. Linkarr does no searching at all: it only
handles links **you** already have, from a file host. Think of it as the import-and-rename half of an
*arr app for the downloads that don't come through Usenet or torrents.

- Point it at the same library folders Sonarr and Radarr use. It uses the same Plex naming, so
  everything lands side by side, and with Plex set up it matches shows by their TMDB id, so a new
  season goes into the folder Sonarr made.
- Its **Duplicate guard** and **Quality upgrade** (Settings → Downloads) skip what you already have and
  replace lower-resolution copies, whichever tool put them there.
- It never deletes anything outright: replaced files sit in a recycle folder for 7 days.

## What it does

- **Paste, share or message a link.** From the web app, the Android Share menu, the Send to Linkarr
  bookmark, or a Telegram bot. Its "/" menu lists the commands; `/help` does too.
- **Paste the whole post.** Paste a forum post as it is (or send it with Send to Linkarr) and Linkarr
  lists every MEGA link in it, what each one is, and its size. Nothing is added until you pick.
  When the same title comes in several qualities only the best is ticked; every season is ticked.
  **Add 3** takes what's ticked, **Add all** takes everything. Seasons of one show become one job, and
  captcha links in the post are held (see below). A password in the post ("Password: abc") is filled in.
- **Send to Linkarr.** Settings → Accounts has a bookmark button. On a computer, drag it to the
  bookmarks bar; on an iPhone, paste the copied code as a bookmark's address; on Android, install
  Linkarr from the browser menu and use Share. Open the post (like it first if the forum hides links
  until you do), select it if you like, and press the bookmark: it sends the links and text on that
  page to your own Linkarr, which opens the picker. It never signs in to or opens the forum itself.
- **Saved archive passwords.** Settings → Downloads → Archive passwords: one per line, tried in order
  on locked archives when the job has none. `{year}`, `{title}`, `{Title}` and `{title_nospace}` are
  filled in from the job, so `{year}` tries `2024` for a 2024 movie. A line with `{year}` is skipped
  when the job has no year. They're left out of the plain backup.
- **Title not recognised.** A link whose name Linkarr can't place shows a **?** instead of a poster and
  a search box: search TMDB, press **Use this**, and the job is renamed before anything is filed.
  **Wrong title?** on a job does the same for one it got wrong.
- **Knows what it is.** Reads the file names inside the link before downloading, finds the title on
  TMDB, and suggests a library: Movies, TV Shows, Documentaries, Docuseries, or whatever you set up.
  TMDB's Documentary genre picks your documentary library.
- **Names it.** `Movie Name (Year)/Movie Name (Year).mkv`, `Show (Year)/Season 2/Show - S02E01.mkv`.
  Patterns are editable.
- **Finds what you already have.** A show you already own gets the new season added to its folder.
  A close match ("The Office" vs "The Office (US) (2005)") asks first. A movie you already have asks:
  replace, keep both, or skip, and says whether the link is an upgrade ("you have it in 1080p, this
  link is 2160p").
- **Matches through Plex.** With Plex set up, Linkarr reads which title each folder is in Plex (its
  TMDB id), so a new season goes to the right show whatever its folder is called, and a one-word
  folder like "Evil" is never mistaken for "Resident Evil". The "Same show?" question shows both
  titles with their years and posters.
- **Links MEGA only partly shows.** When MEGA hides some files in a folder link before downloading,
  Linkarr says so, looks again at what actually arrived, and asks you to check the title before
  filing. Links with no video are flagged and never start by themselves.
- **Links that don't name the show.** A link like `Season 200 - The Science of Connection` has a
  season but no show name. Linkarr asks **Which show is this?** and holds it until you answer with
  **Match to show**: pick a show on TMDB, a show folder you have, or type a new folder name (Plex tags
  like `{tvdb-362158}` are kept). Tick **Do the same for every link named like this** and the next
  ones go there by themselves (Settings → Naming → Remembered shows to forget it). Season numbers
  go up to 4 digits (Season 200, S2019E05).
- **Fix show.** For something already filed under the wrong show, History → **Fix show** moves it into
  the right show and season folder and deletes the old folders once they're empty. A folder with
  anything else still in it is left alone.
- **Search before you download.** Library search shows which library has a title and which seasons.
- **Check, save, done.** Saving a draft's details starts the download; the details fold away until
  you open them again.
- **Several links, one show.** Add more links to a show and, if you like, wait for all of them
  before anything moves.
- **Cleans up.** Unpacks zip/rar/7z (locked ones too, with the password saved on the job), deletes samples and junk, checks the video plays, then moves it.
- **Stuck downloads.** When MEGA's file check fails (the desktop app's "Fix issues"), Linkarr
  re-downloads once by itself, then asks you with **Fix it** / **Skip**, in the app and on Telegram.
- **Safe.** Never overwrites. Replaced files go to a recycle folder for 7 days. Every move is in
  History with **Undo** and **Move to…** (re-file into another folder or library, subtitles
  included). Finished downloads say where they went ("Moved 8 files to …"). Downloads resume after
  a restart.
- **MEGA quota.** Shows how much of your account's transfer allowance is left, and holds a download
  that wouldn't fit until there's room again.
- **Tracker: series.** New season dates, finales and "this show has ended" for the shows you have,
  plus which aired episodes are missing from each show's folder. One button adds every show you
  already have. `/missing` on Telegram lists the gaps.
- **Tracker: movies.** Add a movie you're waiting for and Linkarr tells you when it's out on Blu-ray,
  to buy or rent, or on a streaming service (TMDB's dates and "where to watch" for your country, US
  by default). It drops off the list once it's in your library.
- **Your download site.** Name, address and 1-3 icon letters in Settings → Accounts; it shows as
  a tab in the menu and opens in a new tab. Nothing about it is in the code. `/site` on Telegram
  sends the current address with an Open button.
- **Subtitles.** Optional: a subtitle from OpenSubtitles.com next to each movie and episode, in up to
  three languages, unless one is already there (free API key; your OpenSubtitles login raises the
  daily allowance).
- **Inbox folder.** Anything dropped into it is named and filed the same way.
- **Plex, Jellyfin and Emby.** Plex rescans the right library when something lands, then Linkarr
  checks Plex actually picked it up and tells you if it didn't. Jellyfin and Emby (Settings → Media
  servers: address and an API key, with a **Test** button) scan their libraries too. Use any of them,
  or none.
- **Weekly roundup.** A Telegram message on the day you pick: what was added, which tracked shows
  have new episodes in the next two weeks, and the MEGA allowance left. `/roundup` sends it any time.
- **Clean chat every night (off until you turn it on, Settings → Telegram).** At 4:00 (or the time you pick) the bot
  deletes its chat, your messages too, so each day starts empty. Pinned messages stay, and so do cards that
  still need an answer (captcha links, drafts, questions). `/clear` or **Clear the chat now** does it any time.
  Telegram only lets a bot delete messages from the last 48 hours, and in a group the bot must be an admin
  to delete yours.
- **Captcha links (Settings → Downloads).** Some link-protector pages only show the MEGA link after a
  person solves a captcha. Linkarr never tries to get past it (see below). When it finds one (pasted,
  or inside a Base64 code) it holds the job as **Needs captcha**, sends you the link on Telegram and shows it
  on Activity with **Open link**. Solve it, then paste the MEGA link back: reply to the bot's message, or use
  the box on the job. The job then carries on as usual. One reminder if it's still waiting a day later. List the
  captcha sites you use in Settings → Downloads; none are built in.
- **Library search puts yours first.** Search a title (or tap **Find** on a show in the Tracker) and the
  show you have comes first, with the episodes you're missing. Other titles by that name, like spinoffs,
  are listed below under "Similar names you don't have".
- **Extras (all off until you turn them on in Settings → Downloads):**
  - **Monitored links.** Press **Monitor this link** on a show's MEGA folder link (or send
    `/monitor` and the link to the bot). Linkarr checks that link on a schedule (once a day by
    default, or every 6, 12 or 48 hours), downloads only the files it hasn't seen, and files them in
    the right season. Tracker → **Monitored links** lists each link with when it was last checked
    and how many episodes it has. A link with nothing new for 30 days (you pick) stops by itself,
    and a link the uploader deleted stops too; either way Linkarr tells you.
  - **Missing episode check.** After a show is filed, Linkarr tells you about gaps between the
    episodes you have ("Season 2 is missing episode 3"), once per gap.
  - **Duplicate guard.** Episodes and movies you already have are skipped, whichever link they came
    from. A show link with nothing new isn't downloaded at all.
  - **Quality upgrade.** A higher-resolution copy (1080p over 720p) replaces the one you have without
    asking; the old copy goes to the recycle folder.
  - **Plex folder scan.** Plex scans only the folder that changed, so new episodes show up sooner.
    Plex's own path for the library is read from Plex, so it works when Plex sees the share under
    another name (or runs on Windows).
  - **Download window.** Big downloads (5 GB or more by default) wait for the hours you set, 01:00
    to 07:00 by default. A download that has started keeps going.
  - **Morning summary.** One Telegram message at the time you pick: what came in overnight, one
    title per line, plus what's still downloading and what needs you. Nothing after a quiet night.
- **Themes.** Dark (default), Light, or match your device. Settings → System.
- **One Save.** Each Settings page has one **Save changes** bar that says when something is unsaved.
- **Phone layout.** Pages are tabs along the bottom: Activity, Library, Tracker, your download site
  (History when none is set) and Settings. On a phone, Settings pages are a drop-down.
- **Forgot your password?** Run `docker exec linkarr python -m linkarr reset-password`. It signs
  everyone out and prints a new setup code; your settings, libraries and history stay.

Not included: FileCrypt or other link protectors, searching for links, multiple MEGA accounts.

## Captcha links are held, never bypassed

Some share links sit behind a page with a captcha. Linkarr does not solve, skip or work around
captchas, and it never will. When it sees a link on one of the captcha sites you list in
Settings → Downloads, it holds the job as **Needs captcha** and hands you the link. You open it, solve it
yourself, and paste back the MEGA link the page gave you. There are no settings or variables that
get through a link protector automatically, and none are planned. Pull requests that automate captchas or
link protectors won't be accepted.

## Quick start

`docker-compose.yml`:

```yaml
services:
  linkarr:
    image: ghcr.io/xsil0/linkarr-app:latest
    container_name: linkarr
    restart: unless-stopped
    ports:
      - "9040:9040"
    environment:
      - TZ=Europe/Rome          # your time zone
      - PUID=1000               # user and group that own your media folders
      - PGID=1000
      - LA_COOKIE_SECURE=1      # 0 only if you open it over plain http
    volumes:
      - ./linkarr-data:/data    # database, keys, MEGA session, logs, backups
      - /path/to/media:/media   # the one folder that holds Movies, TV Shows and so on
```

```
docker compose up -d
docker logs linkarr          # the one-time setup code is here
```

Open `http://<server>:9040`, enter the setup code and create your account. Then:

1. **Settings → Libraries:** add each library folder (relative to `/media`, so `Movies` means
   `/media/Movies`).
2. **Settings → Accounts:** sign in with your MEGA account and add a free TMDB API key.
3. **Settings → Media servers** (Plex, Jellyfin, Emby) and **Telegram** are optional.
4. **Settings → Setup check** shows anything still missing.

`/media` should be the one share that holds all your libraries: Linkarr downloads into a hidden
`.linkarr` folder on it, so filing a finished download is an instant move, not a copy. There's also
[docker-compose.example.yml](docker-compose.example.yml) with an [.env.example](.env.example), and a
step-by-step [Unraid guide](docs/unraid.md).

Use HTTPS (a reverse proxy or `tailscale serve`) and keep Linkarr off the open internet. It holds your
MEGA session. See [SECURITY.md](SECURITY.md).

## Settings reference

### Container

| Variable | Default | What |
|---|---|---|
| `TZ` | `UTC` | time zone (download window, summaries, log times) |
| `PUID` / `PGID` | `99` / `100` | user and group Linkarr runs as (Unraid's defaults) |
| `LA_PORT` | `9040` | web port |
| `LA_COOKIE_SECURE` | `1` | `0` to sign in over plain http |
| `LA_SESSION_HOURS` | `336` | how long a sign-in lasts |
| `LA_TRUSTED_PROXIES` | `*` | addresses whose `X-Forwarded-For` is believed (for the sign-in lockout). Set it to your reverse proxy's address if Linkarr can be reached other than through it |

| Path | What |
|---|---|
| `/data` | database, encrypted keys, MEGA session, logs, daily backups |
| `/media` | your media share: library folders, the Inbox and Linkarr's work folder |

### In the app

Everything personal is set in the app and kept in `/data`, never in the image or the repository. Keys
and tokens are encrypted at rest with `/data/secret.key`.

| Settings tab | What's there |
|---|---|
| Setup check | what's set up and what's missing |
| Libraries | your library folders, Movies or Shows, documentary libraries, Plex section ids |
| Accounts | MEGA sign-in (two-factor too) and quota; TMDB key; your download site's name and address; the Send to Linkarr bookmark |
| Media servers | Plex address and token, folder-only scans; Jellyfin and Emby address and API key |
| Telegram | bot token, allowed chat ids, messages, weekly roundup, morning summary, nightly chat clear |
| Downloads | auto-start, saved archive passwords, unpacking and cleanup, duplicate guard, quality upgrade, captcha sites, download window, monitored links, Inbox folder, subtitles |
| Naming | movie, show, season and episode patterns; remembered shows |
| System | theme, password, backup, Base64 decoder, logs |

The plain backup leaves out keys, tokens and saved archive passwords; **Include keys, tokens and saved
passwords** puts them in.

## Your responsibility

Linkarr is a tool for organizing files you have the right to download and keep, such as your own
uploads, files shared with you by their owner, or content that is free to share. You are responsible
for what you download with it and for following the law where you live and the terms of the services
you use (MEGA, TMDB, Plex, Telegram, OpenSubtitles). Linkarr doesn't find, host or share any content,
and it isn't affiliated with any of those services.

## Development


```
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
LA_DATA=./data LA_MEDIA=./media LA_COOKIE_SECURE=0 python -m linkarr
```

The tests use fakes for MEGA, TMDB, Telegram and Plex; nothing touches the internet.

## License

[MIT](LICENSE). The bundled fonts, IBM Plex and Big Shoulders Display, are under the SIL Open Font
License; the licence texts are in `linkarr/web/fonts/`.
