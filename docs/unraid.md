# Running Linkarr on Unraid

The image is `ghcr.io/xsil0/linkarr-app`, built by GitHub Actions on every push to `main`.

| Tag | What |
|---|---|
| `latest` | newest build of `main` (use this) |
| `0.1.0` etc. | the version in `linkarr/__init__.py` |
| a short commit id | one exact build, for going back |

## 1. The container

Docker → **Add Container**, **Advanced View** on:

| Field | Value |
|---|---|
| Name | `Linkarr` |
| Repository | `ghcr.io/xsil0/linkarr-app:latest` |
| Network Type | `Bridge` |
| WebUI | `http://[IP]:[PORT:9040]` |

| Type | Name | Container | Host / Value |
|---|---|---|---|
| Port | Web | `9040` | `9040` |
| Path | Data | `/data` | `/mnt/user/appdata/linkarr` |
| Path | Media | `/media` | the share that holds your library folders, e.g. `/mnt/user/Media`, **Read/Write** |
| Variable | TZ | | your time zone, e.g. `Europe/Rome` |
| Variable | PUID | | `99` (Unraid's `nobody`) |
| Variable | PGID | | `100` (Unraid's `users`) |

**Media** must be the one share that contains Movies, TV Shows and the rest. Linkarr downloads into a
hidden `.linkarr` folder on that same share, so moving a finished download into a library is an
instant rename, not a copy. Library folders are set in the app, not here.

**Apply.** The log (Docker tab → Linkarr icon → **Logs**) shows:

```
No account yet. Open Linkarr and use this setup code: 3f9c1e7a
```

## 2. HTTPS

The sign-in cookie only works over HTTPS. With Tailscale on the server:

```
tailscale serve --bg --https=11443 http://localhost:9040
```

Then open `https://<server>.<tailnet>.ts.net:11443`. Don't use `tailscale funnel` and don't forward
the port on your router: Linkarr has your MEGA session in it.

Plain `http://<server-ip>:9040` on your home network works too, with nothing to set. If you only
ever use HTTPS, you can add the variable `LA_COOKIE_SECURE` = `1` so the sign-in never travels
over plain http.

## 3. First run

1. Open Linkarr, enter the setup code, pick a username and a password (10+ characters).
2. **Settings → Libraries**: add each library folder. The folder is relative to the Media path,
   so `Movies` means `/mnt/user/Media/Movies`. Pick Movies or Shows, and tick **Docs** on your
   documentary libraries. **Load Plex libraries** lists Plex's section ids for the last column.
3. **Settings → Accounts**: sign in to MEGA (with your two-factor code if you use one). Downloads then use your
   account's transfer quota. Linkarr doesn't keep the password; MEGA's own tool keeps a session in
   `/data/megacmd`, so updating the container doesn't sign you out.
4. **Settings → Accounts**: a free TMDB API key (themoviedb.org → Settings → API). Optional:
   **Media servers** (Plex, Jellyfin or Emby) and **Telegram** (bot token and chat id).
5. **Setup check** shows anything still missing.
6. On Android: Chrome menu → **Add to Home screen**. After that, **Share** a MEGA link from any
   app and pick Linkarr.

## 4. Updating

Docker tab → **Check for Updates** → **Apply Update**. Your settings, MEGA session and history are in
`/data` and stay. Linkarr also writes a daily copy of its database to `/data/backups` (last 7 days).

## Moving off a desktop MEGA sync

If you used the MEGA desktop app to sync a folder and renamed things by hand: stop that sync once
Linkarr is set up, and point the old sync folder (or anything else that drops finished downloads) at
the **Linkarr Inbox** folder on the Media share instead. Linkarr names and files whatever lands
there, asking first like it does for links.
