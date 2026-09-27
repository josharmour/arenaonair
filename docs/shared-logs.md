# Sharing logs between two players

Each Arena client writes only what its own player can see: their hand, their
draws, and the public board. A booth that reads **both** players' logs can
see both hands, which is how TV poker's hole-card cameras work. That is also
the risk: a player who can hear it could learn the opponent's hand.

## Who should listen

| Setup | Hands on air | Use it for |
|---|---|---|
| Each player runs ArenaOnAir on their own log (the default) | Only your own, as hole-card commentary | Playing live. Each player gets their own booth. |
| One machine merges both logs, listener is **not** a player | Both, with `--spectator` | A caster, a friend watching, a tournament stream |
| One machine merges both logs, listener **is** a player | None: the booth refuses | Not supported. It would be cheating. |

When logs are shared (`--log-player1/--log-player2` or `--relay-listen`), the
booth keeps every hand off air unless you pass `--spectator`. This holds even
with `--hole-cards on`. If a spectator booth is also streamed, the usual rule
still applies: you need `--stream-delay` of at least 30 seconds.

## Setting up a spectator booth (relay)

1. **Choose a network.** The relay uses plain WebSockets (`ws://`) with a
   shared secret, and the stream carries each player's full log, including
   their hand. Don't expose it on the open internet. The easy safe option is
   [Tailscale](https://tailscale.com): all three machines join one tailnet,
   and traffic is encrypted end to end without opening router ports. A home
   LAN works too.
2. **On the caster machine** (not a player):

   ```bash
   arenaonair --relay-listen 0.0.0.0:8765 --relay-secret "$SECRET" \
              --broadcast-mode dual --spectator --stream-delay 60 --overlay-port 8787
   ```

3. **On each player's machine**, forward their log. Arena needs Detailed Logs
   turned on (run `arenaonair doctor` to check):

   ```bash
   python -m arenaonair.relay --connect CASTER_TAILNET_NAME:8765 --secret "$SECRET"
   ```

   `--log-path` is optional; the Player.log location is auto-detected.

The first client to connect starts the broadcast. Seats come from the game
itself, not from connection order. If one player drops, the booth continues
with public information plus whatever the remaining log shows.

## What the players need to agree to

Forwarding your log shows the caster your hand and decklist for the whole
match. Only do it with a caster you trust, and only when both players have
agreed. Check your event's rules too: some organised play forbids third-party
tools that receive live game data.
