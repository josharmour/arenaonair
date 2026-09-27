# Using ArenaOnAir

Install the app using the [README](../README.md#install). Commands below use
`arenaonair` from an activated Python environment. With the recommended launcher,
replace it with `bash ./run.sh` on macOS/Linux, or
`powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1` on Windows.

## Booth and settings

Two commentators are the default, even with one Arena log. The play-by-play
caster calls the action; the analyst adds card explanations and observations.
The [generative booth](llm-booth.md) writes original exchanges from observed game
facts. Use **Connect / subscription…** for five free hosted matches, a Patreon
subscription, or your own provider. Hosted generation uses GLM 5.3 and requires
internet access; only voice synthesis runs on your computer by default.

The Broadcast tab's **Booth** section lets you choose **Two casters** or
**Play-by-play only**, choose voices, and set the **Focus** to **Mostly calls**,
**Balanced**, or **Mostly analysis**. **Hear the booth** previews the voices
between games. Caster names follow the selected voice unless you customize them.

```sh
arenaonair --broadcast-mode solo
arenaonair --persona esports
arenaonair --focus analysis
```

Personas are `classic`, `esports`, `test_match`, and `pro_tour`. They set voices,
pace, and the AI writer's style. The AI booth's focus controls how often the
analyst discusses the public position. Coaching is off by default; enabling
**Coaching** lets the AI suggest plays using only information you can see.

The **Settings** tab covers the writer, style, pacing, voices, log path, hand
commentary, history, OBS, AI connection, and shared logs. Changes save to
`~/.arenaonair/config.toml`. Settings marked ↻ require **Restart now**; others
apply to subsequent commentary. Explicit launch flags override saved settings.

## Match history and recaps

Results, opponents, and observed cards are kept locally in
`~/.arenaonair/history.sqlite`. A text recap is written under
`~/.arenaonair/recaps/` after each game. Use **Speak last recap** in the window,
or render an audio file:

```sh
arenaonair recap --audio
```

Audio rendering needs the voice dependencies. Add `--llm` to use your configured
model to write the recap. Use `arenaonair recap --help` for output options.

## Streaming with OBS

```sh
arenaonair --overlay-port 8787 --stream-delay 30
```

Add `http://127.0.0.1:8787/` as an OBS Browser Source for captions, a scoreboard,
and your hand when allowed. Use `?show=captions,scoreboard` to select panels.
Capture commentary audio with the appropriate OBS audio source for your OS.
Configure the same stream delay in OBS: ArenaOnAir's delay setting records your
choice for hand privacy; it does **not** delay the OBS output for you.

On your own machine, the AI booth may discuss your visible hand. When streaming
is enabled, automatic hand commentary stays off unless the declared delay is at
least 30 seconds. `--hole-cards off` disables it explicitly. Avoid overriding the
gate with `--hole-cards on` when opponents might hear the stream.

## A second log or relay

One log is enough for both casters. Two compatible logs can add information for
a spectator booth:

```sh
arenaonair --log-player1 /path/to/first.log --log-player2 /path/to/second.log --spectator
```

Either numbered slot can work alone. Sources only share private information
when match, game, GRE state, and public board agree. Shared-log routes suppress
all hand commentary unless the listener is marked as a spectator. See
[sharing logs](shared-logs.md) for the relay setup and player consent.

## Diagnostics

```sh
arenaonair doctor
arenaonair --dry-run --no-ui
arenaonair --help
```

`doctor` checks local prerequisites. `doctor --online` also sends a request to
your configured model and may incur a provider charge. `--dry-run` replaces
speech with printed commentary; with AI configured, model requests still run.
`--narration-mode legacy` explicitly chooses built-in commentary.

The window's **Copy bug report** copies recent logs and diagnostic status to the
clipboard. It does not upload them, include raw Player.log/deck/hand data, or
include AI speech that may mix public and private facts. Reports start collecting
when the app launches. Review the report before posting a GitHub issue.

See [troubleshooting](install.md#troubleshooting) for log discovery, model errors,
missing sound, and desktop-launch problems.
