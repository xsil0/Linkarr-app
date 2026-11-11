# Security

## Reporting a problem

Please don't open a public issue for a security problem. Use GitHub's private reporting instead:
**Security → Report a vulnerability** on this repository. Say what you found, how to reproduce it and
which version (`linkarr/__init__.py`, or the image tag) you ran.

You'll get a reply within a week. A fix ships as a new image tag, and the release notes credit you
unless you'd rather not be named.

## Supported versions

Only the newest release (the `latest` image) gets fixes. Update before reporting if you can.

## How Linkarr is meant to run

Linkarr holds your MEGA session and your Plex and Telegram tokens, so treat it like any other admin
tool on your server:

- **Keep it on your own network.** Reach it from outside through a VPN such as Tailscale or WireGuard.
  Don't forward its port on your router and don't put it on a public tunnel.
- **Use HTTPS** (a reverse proxy or `tailscale serve`). The sign-in cookie is marked secure by default;
  `LA_COOKIE_SECURE=0` is only for plain http on a trusted home network.
- **One account.** The first visit needs the setup code from the container log. After that there are
  no sign-ups. Passwords are at least 10 characters and stored hashed.
- **Secrets stay in `/data`.** TMDB, Plex, Telegram and OpenSubtitles keys are entered in the app and
  encrypted at rest with `/data/secret.key`. Nothing personal is in the image or this repository.
  Back up `/data` somewhere private, and never commit it.
- **Telegram.** The bot only answers the chat ids listed in Settings → Connections.
- **Logs** hide MEGA link keys and tokens, so they're safe to paste when asking for help.

## In scope

The web app and its API, the Telegram bot, the Docker image, and how secrets are stored.

## Not in scope

Problems in MEGA, MEGAcmd, Plex, TMDB, Telegram or OpenSubtitles themselves (report those to them),
and setups that put Linkarr directly on the internet against the advice above.
