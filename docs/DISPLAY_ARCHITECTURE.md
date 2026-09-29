# Cube display foundation, carousel, Clock, Weather, Animation, and Music

The Raspberry Pi has one production HUB75 owner: `client/native/cube-display/cube-display.cc`.
Its existing process initializes RGBMatrix and RP1 PIO, owns the 64×64 geometry,
pixel mapping, 33 ms frame pacing, VSync, brightness output, blanking, and shutdown.
Display apps and composition code include no matrix or GPIO headers.

## Frame path

`display-frame.h` defines the sole native `Rgb`, 64×64 `Frame64`, `Mask64`,
dimensions, pixel count, and indexing. The normal path is:

      TetrisClockApp, WeatherApp, AnimationApp, or MusicApp
            │
            ▼
    DisplayAppRegistry
            │
            ├── registered DisplayApps (availability checked at selection)
            ▼
      CarouselScheduler ← ordered native carousel configuration
            │
            ▼
       active app slot
            │
            ▼
      DisplayRuntime
            │
            ▼
         Frame64 (black if no available app)
            │
            ▼
      CubeStateOverlay
            │
            ▼
        compositor
            │
            ▼
    explicit content and existing dissolve
            │
            ▼
     master brightness / output blanking
            │
            ▼
       cube-display
            │
            ▼
        HUB75 64×64

`DisplayApp::Available(const DisplayContext&)` reports whether already cached
data can be shown. `Render(const DisplayContext&, Frame64*)` writes pixels into
caller owned storage. `DisplayContext` currently contains monotonic time and frame
duration. An app performs no I/O, audio, interaction management, hardware access,
or scheduling. `DisplayRuntime` holds one non owning optional app pointer. With
no available app, it returns a black `Frame64`; the old ambient animation was
intentionally removed and is never used as fallback. Black pixels are submitted
through the normal frame path and differ from display off or brightness zero.

The existing Pi `Coordinator` remains the authoritative interaction owner. Its
generation checks reject stale updates before state reaches the native socket.
The native parser maps wake/listening, thinking, speaking with level, follow-up,
and idle into an immutable `PresentationState`. `CubeStateOverlay` only changes
pixels in a copy of the app frame. Its straight gray bottom-row line is hidden
at idle, reveals from the center on listening/wake, carries an RGB-white moving
highlight while thinking, varies with speech level, and stays steady during follow-up.
These deterministic effects use elapsed state time supplied by the renderer;
their exact appearance remains isolated from interaction ownership.
There is no second voice state machine.

Explicit text or icon content has complete frame ownership above the app and
overlay. The Pi volume adapter uses it for solid white `MUTE`, `VOL`, or percentage
text, refreshing its two-second window at playback. `ContentRenderer` retains
the one existing 900 ms spatial dissolve for forming, morphing, and releasing
content. `dissolve-transition.h/.cc` contains its hardware free per-pixel effect;
the content layer owns masks, target state, and submitted-frame snapshots. No
general animation framework was added. Perlin, animated palette colors, and
the former full-screen ambient texture are gone.

The output boundary applies one `master_brightness_percent` directly to
RGBMatrix. `0` clears submitted pixels because the matrix library accepts only
1–100. `25`, `50`, and `100` set those physical percentages exactly. Startup
master is initialized from the parsed `--led-brightness` option; the repository
service template supplies `50`, and the compiled default is `10`. The
installed Pi unit may use different flags until the coordinated naming
migration. Display off is
an independent visibility flag. `set_animation` and the redundant scaled
brightness status field were removed from the control contract.

## Registry, ownership, and configuration

Production registers `TetrisClockApp` as `tetris-clock`, `WeatherApp` as
`weather`, `AnimationApp` as `animation`, and `MusicApp` as `music`, in
15-second/15-second/10-second/10-second
carousel order. All apps and their
sources live at renderer scope before the registry, scheduler, and runtime.
Volume and Cube presentation states are not apps. Empty schedules still
produce a black app frame.

`DisplayAppRegistry` associates owned, case-sensitive string IDs with non-owning
`DisplayApp*` references in an ordered map. `Register(id, app)` returns false for
empty or duplicate IDs, preserving the original registration. `Find(id)` returns
the registered pointer or null; `Contains(id)` reports existence. No registration
order determines carousel order. IDs are copied into registry storage; apps are
neither copied nor deleted. App objects must keep stable addresses and outlive
all registry, scheduler, and runtime use. Future apps should be declared before
these objects at renderer-lifetime scope. The registry must also outlive its
scheduler. There is no removal API or shared ownership.

`CarouselEntry` contains `std::string app_id` and
`std::chrono::steady_clock::duration duration`. An ordered
`std::vector<CarouselEntry>` specifies participation, order, and independent dwell
durations; omitting an ID disables that app. Duration belongs to configuration,
not to `DisplayApp`. Production uses `{{"tetris-clock", 15s}, {"weather", 15s},
{"animation", 10s}, {"music", 10s}}`.
The schedule has no environment, file, HTTP, UI, or voice configuration.

`CarouselScheduler::Configure(entries, error)` resolves pointers once and returns
false with an optional descriptive error for empty IDs, unknown IDs, duplicate
schedule IDs, or zero/negative durations. Rejection leaves the previous schedule,
selection, and timer intact; it does not crash the renderer. An unavailable but
registered app is valid configuration. An empty schedule is valid. Successful
replacement restarts in configured order with a fresh dwell on the next update,
or clears selection if empty. Even if that chooses the same pointer, the new
configuration restarts its dwell without resetting the runtime app slot.

## Scheduling and presentation time

`CarouselScheduler::Update(context, presentation_suspended)` only selects an app
pointer. It runs on the existing render loop, uses monotonic `context.now`, and
owns no thread. Registry registration and configuration replacement also run on
that thread, outside frame updates. Updates allocate no containers or strings and
perform no registry lookup, rendering, networking, or hardware access. There are
no per-app processes, external plugins, provider workers, or per-frame logs.

The first selection is the first available configured app. At exact dwell expiry,
the scheduler scans forward in configured order, wrapping and skipping unavailable
apps. An active app becoming unavailable advances immediately, giving the next
available app a full dwell. All-unavailable or empty schedules select null, so
the existing runtime returns black. Recovery from no selection starts scanning at
the first configured entry. Newly available apps never interrupt an available
current app; they rejoin when their position is reached. A sole available app
keeps its pointer continuously while its dwell is renewed.

One update can change selection at most once. Long stalls and large forward time
jumps advance only to the next available entry and discard elapsed overshoot;
the new dwell begins at that update's monotonic time. There is no catch-up burst.
Tests inject time directly and never sleep to advance the carousel.

The renderer applies pending explicit-content commands before scheduling and
passes the generic suspension signal for explicit content or a music playback notice. Dwell
pauses during forming, holding, morphing, and releasing content, including the
existing 900 ms dissolve. Time before pause entry is charged; paused intervals
are not charged on resume. Six visible seconds of a 15-second dwell leave nine
seconds after a two-second suspension. The first tick observing `Full` resumes
timing, so release completion has frame-level precision. Repeated content updates
do not restart an app's dwell. If expiry coincides with pause entry, timer-driven
rotation waits until resume.

While suspended, selection remains unchanged unless availability requires an
immediate replacement or clearing. A replacement receives a full dwell frozen
until resume; recovery from no available app is also checked during suspension.
Display off and master brightness zero do not independently pause the carousel:
the existing logical render path continues behind output blanking. Voice state
overlays likewise do not affect scheduling or durations.

The renderer compares the selected pointer against the pointer last installed in
`DisplayRuntime` and calls `SetActiveApp` only when different. Runtime calls the
default no-op `DisplayApp::OnActivated(context)` hook before the first render
after a pointer change. Runtime remains unaware of IDs, durations, or rotation
rules and renders each frame normally.
App changes are direct switches, with no app-to-app transition. The content
layer retains exclusive ownership of its original solid-white presentation and
dissolve; the scheduler knows neither volume nor transition details.

## Spotify music display

`apps/music.h/.cc` consumes only the renderer-owned `MusicState`. Artwork fills
a 58×58 viewport at x=3..60, y=0..57, with three-pixel side margins. The full
cover stays visible with its aspect ratio preserved. Rows 58–59 are a two-pixel
gap before the controls. The play indicator uses x=3..5, y=60..62; pause uses
columns 3 and 6 on those rows. The 51-pixel progress bar uses x=10..60, y=61.
Row 63 stays black for the existing voice overlay. Missing art uses the existing
music-note placeholder. Unknown duration or progress hides the bar without
moving the layout.

Adjust the layout constants at the top of `client/native/cube-display/apps/music.cc`:
`kArtworkSize` controls the square viewport size; `kArtworkTop` its first row;
`kControlsGap` the blank rows before controls; and `kProgressOffset` the bar's
start relative to the artwork's left edge. Horizontal centering, control position,
and bar width are derived from these settings. The three-row play/pause glyph
has a fixed design. Compile-time checks keep the layout above voice row 63.
Rebuild with `make -C client/native/cube-display -j2` after changing settings.

The backend's `prepare_artwork()` still centers aspect-preserving artwork in
x=8..55, y=6..53 of a 64×64 RGB frame. The renderer uniformly enlarges that
entire viewport to 58×58 with nearest-neighbor pixel-center sampling, including
its letterboxing and black artwork pixels. It does not crop or detect image
edges. Controls align with the viewport width; non-square art retains padding.
The source still contains at most 48×48 pixels of detail. This layout requires
only a Pi renderer update; no backend or protocol change is required. The
candidate build is not installed automatically.

Fresh playback on Cube makes music available in its normal carousel position.
Starting playback, resuming, and track changes show music immediately for five
seconds; repeated playing updates and buffering recovery do not extend it.
A fresh playing-to-paused edge shows
music immediately for five seconds, temporarily suspending the carousel. The
interrupted entry then resumes its remaining dwell; repeated pause packets do
not extend the notice. Resume replaces it with a playing notice. Initial paused state does not trigger
a notice. Buffering freezes progress. Stale/disconnected/transferred playback
removes music availability. Explicit content, blanking, brightness, and voice
composition retain their existing priorities. Hidden pause notices expire in
wall time rather than replaying later.

The optional Pi `display/music.py:MusicProvider` polls authenticated backend
display endpoints and samples the existing Soloist observer. Network requests
and one-second local publication run concurrently in its background event loop.
No renderer network work, image decoding, or playback control is added. Backend
state remains owned by `MusicController`. See [Spotify protocol](SPOTIFY.md) for
the wire contract, limits, and physical acceptance sequence.

## Tetris Clock + Date

`apps/tetris-clock.h/.cc` implements the normal `DisplayApp` contract. The path is
`TetrisClockApp → DisplayAppRegistry → CarouselScheduler → DisplayRuntime → Frame64`;
the existing overlay, explicit-content dissolve, brightness, and physical output
follow unchanged. The app does not own a frame loop, hardware, threads, audio,
brightness, or network access and does not know about scheduling or volume.

`LocalClockSource::Read(std::tm*)` is a small injected boundary. Production uses
`SystemLocalClock` with the Pi's system time and local timezone via `time`,
`tzset`, and `localtime_r`; timezone state is refreshed before conversion so a
running renderer observes timezone changes. Tests supply deterministic local times. Wall time selects
24-hour `HH:MM` and an English date such as `SAT 26 SEP`. Seconds are not displayed.
Availability is always true because no provider is required. Failed conversion
or invalid fields produce a black frame, retain prior targets, and retry next
render without crashing or forcing scheduler churn.

Geometry and palette live in `apps/tetris-clock-geometry.h`. This is an original
implementation: [TetrisAnimation](https://github.com/toblum/TetrisAnimation) was
consulted only for the high-level falling-number behavior. No external source,
digit tables, algorithms, comments, assets, or dependencies were copied or ported.
The original geometry is constructed at compile time from tessellated strokes:
horizontal bands contain an I and two mirrored L/J tetrominoes, vertical stems
use interlocking L/J pairs, and O tetrominoes fill unconnected stroke ends. Every
piece has exactly four connected cells, with no shared final cells. There is no
runtime geometry solver, randomness, or game simulation.
The `1` has a centered stem and full-width foot, so all ten digits occupy both
outer columns of their cells. `kClockXOffset` in the geometry header is the
single source constant for moving the time and colon horizontally; negative
values move left and positive values move right. Its default is zero. Date
placement stays independent and unchanged.

The logical layout uses square 2×2-pixel blocks. A digit occupies a 6×14-cell
(12×28-pixel) box. Base digit boxes begin at x=3,17,35,49, y=11, ending at y=38;
the time group spans x=3–60, leaving three black pixels on each side. The two
neutral-white 2×2 colon dots start at (31,19) and (31,29), sharing the same
horizontal clock offset, and stay steady. Rows 0–10 provide falling space.
Rows 48–62 form a full-width solid-white date stripe. `SAT 26 SEP` is a centered
59×9-pixel black cutout starting at (2,51), with bold strokes and open letter
counters. Nine black rows separate the clock from the stripe. The bottom overlay
row remains free. All coordinates are logical; physical rotation
remains outside the app. Background is pure black with flat palette colors,
integer pixel positions, and no filtering, gradients, or per-app brightness.
The date-only constants in `apps/tetris-clock-geometry.h` control the stripe's
vertical position (`kDateY`), the cutout's relative x/y offsets, and its RGB
stripe color (`kDateStripeColor`). Lowering all three color components together
dims only the date stripe; master brightness remains controlled at the output
boundary. Compile-time bounds checks prevent moving the date out of the stripe,
over the clock, or onto the bottom overlay row.

The prior explicit-content font is centered, scaled, and limited to five glyphs,
so it remains unchanged. `pixel-font.h` supplies a small hand-drawn 5×9 bold font for
digits and English date letters, with a one-pixel character gap and clipped
native frame drawing. The date is a black cutout in the white stripe and never animated.
Its character buffer updates only when the visible local date changes.

Each of four digits retains its target and monotonic start timestamp. On first
render, current targets begin unassembled. Up to 17 rigid pieces per digit fall
vertically from above the frame into fixed targets, with lower pieces scheduled
first. All digits build concurrently. `kAssemblyTime` (2800 ms) and `kFallTime`
(600 ms) in the implementation centralize timing; stagger spacing is derived
from each digit's piece count. Motion is integer linear travel derived from
`context.now - started`, independent of frame count and `context.elapsed`.
Final positions clamp at their targets and stay stable. The renderer owns frame
storage; app state uses fixed arrays and rendering creates no dynamic containers
or strings. Digit targets are precomputed at compile time, never rebuilt per frame.

Every render samples wall time once and compares four target values. Minute
changes immediately clear and rebuild only changed digit positions; unchanged
digits retain both their pixels and start timestamps. A time correction, date
rollover, or jump during animation replaces the current target directly, once
per render, without replaying intermediate minutes. Animation never uses wall
clock deltas. Date changes do not reset digits. A single available carousel entry
retains its pointer at dwell renewal, so the clock never restarts every 15 seconds.
Clock does not override the activation hook: its state persists through carousel
re-entry, output blanking, and explicit-content suspension. While hidden, the
existing runtime continues rendering, and wall time and animation remain current.

## Weather data and availability

`cube.py` starts and closes one `WeatherProvider` background worker. It uses
HTTPS to query the [Open-Meteo Forecast API](https://open-meteo.com/en/docs),
validates the response, and retains one immutable `WeatherSnapshot`. HTTP and
JSON parsing never occur in native frame rendering:

    Open-Meteo → WeatherProvider → cached WeatherSnapshot
        → authenticated @cube-display datagram → native WeatherState
        → WeatherApp → DisplayAppRegistry → CarouselScheduler
        → DisplayRuntime → Frame64 → existing composition and HUB75 output

Default location is Braunschweig: latitude `52.2772`, longitude `10.5244`,
timezone `Europe/Berlin`. `CUBE_WEATHER_LATITUDE`, `CUBE_WEATHER_LONGITUDE`,
`CUBE_WEATHER_TIMEZONE`, and `CUBE_WEATHER_REFRESH_SECONDS` override those
values; refresh defaults to 600 seconds and accepts 120–1800. Invalid
configuration makes Weather unavailable without preventing voice startup.
No key or geolocation is used.

The request selects `current=temperature_2m,weather_code,is_day` and
`hourly=precipitation_probability`, with Celsius, Unix timestamps,
`forecast_hours=2`, and `past_hours=1`. The provider selects the unique hourly
interval containing the current timestamp, including across local daylight
saving changes. It rejects malformed or oversized responses, errors, and
timeouts. Connect timeout is 3 seconds, read timeout 5 seconds, overall request
deadline 10 seconds, and decoded response limit 32 KiB. A successful fetch is
followed by another after the configured refresh interval; failures retry after
60 seconds. Weather retains the last good snapshot for less than 3600 seconds.

The Python snapshot contains temperature in tenths of Celsius, integer
probability 0–100, a normalized condition, daylight, and the successful
monotonic update time. The provider maps WMO codes as follows:

| Condition | WMO codes |
| --- | --- |
| Clear | 0 |
| PartlyCloudy | 1, 2 |
| Cloudy | 3 and unknown future numeric codes |
| Rain | 51, 53, 55, 56, 57, 61, 63, 80, 81 |
| HeavyRain | 65, 66, 67, 82 |
| Thunderstorm | 95, 96, 97, 99 |
| Snow | 71, 73, 75, 77, 85, 86 |
| Fog | 45, 48 |

The canonical local socket carries `weather v1 <temperature_c10>
<probability> <condition_id> <is_day> <lease_seconds>`. IDs 0–7 follow the
table order. The provider republishes cached data every 60 seconds, including
after renderer restart. Its lease is the smaller of 1800 seconds and the
remaining snapshot lifetime, rounded down. Republishing cannot extend the
one-hour source-data window. The native receiver validates exact format and
bounds in the existing 128-byte datagram, after the existing UID and truncation
checks. Native `WeatherState` stores one snapshot and its monotonic receipt
expiry. It has no worker or network code. `WeatherApp::Available()` reads this
lease; when absent or expired, the scheduler skips Weather and leaves the clock
active. Recovery joins at the next normal scheduler opportunity without
interrupting the current clock dwell.

`WeatherApp` renders into the caller's 64×64 frame: its animated scene is
clipped to x=4–59, y=0–27; 2× compact-font temperature appears at y=30–43,
with the number centered and the degree mark to its right. A full-width blue
stripe occupies rows 48–62, exactly the Clock date stripe's 64×15 bounds.
Centered black pixel-font `RAIN NN%` or `SNOW NN%` is cut out at y=51–59.
Rows 28–29 and 44–47 separate content; row 63 remains for the interaction overlay.
Background is pure black. Clear day rays and clear night stars pulse subtly;
partly cloudy and cloudy clouds drift; rain and snow use fixed falling lanes;
heavy rain moves faster and uses more lanes; thunder flashes a fixed bolt for
180 ms every eight seconds; fog bands slide at different periods. Probability
selects sparse, medium, or dense precipitation lanes at 0–33%, 34–66%, and
67–100%. All movement is a deterministic function of `DisplayContext::now`.
There is no per-frame heap allocation, randomness, activation reset, or app
brightness control. The existing explicit content, 900 ms dissolve, master
brightness, and physical rotation retain their established ownership.

## Local GIF conversion and AnimationApp

GIFs are development inputs only. The production path is:

    source GIF → offline Pillow converter → .cubeanim v1
        → startup AnimationCatalog → AnimationApp → registry → carousel
        → Frame64 → existing overlay, explicit content, brightness, and HUB75 output

The tracked source GIFs are in `client/assets/animations/source/`. The four
tracked runtime files—`eyes`, `ghost`, `heart`, and `rocket`—are original pixel art
generated by `tools/generate_cube_animation_samples.py` and converted through
`tools/convert_cube_animation.py`. Runtime files are the top-level
`client/assets/animations/*.cubeanim`; the loader does not scan subdirectories.
The installed service's working directory is `client/native/cube-display`, so
its startup-only catalog path is `../../assets/animations`. There is no runtime
GIF decoder, network request, Python dependency, new service, or other matrix
owner. The catalog is fixed until the renderer is next started.

`.cubeanim` v1 is uncompressed. A 16-byte header contains ASCII `CUBEANIM`
(8 bytes), then little-endian unsigned 16-bit version `1`, width `64`, height
`64`, and frame count. Each frame contains a little-endian unsigned 16-bit
duration in milliseconds followed by 4096 row-major little-endian RGB565
pixels (red in bits 15–11, green 10–5, blue 4–0). The converter drops the low
three red/blue bits and low two green bits; playback repeats the high bits when
expanding to RGB888. There is no alpha or compression in the native format.

Limits: 1–64 frames, 20–2000 ms per frame, at most 60,000 ms total, and exact
file size `16 + frame_count × 8194` bytes. Maximum file size is 524,432 bytes.
The loader rejects bad magic, version, dimensions, count, duration, truncation,
trailing bytes, or inconsistent size before the app can render. It sorts names,
skips invalid files independently, and retains at most eight valid files. Those
files occupy at most 4,195,456 retained bytes plus small metadata; startup may
use one additional 524,432-byte file buffer. If none is valid,
`AnimationApp::Available()` is false and the existing scheduler skips it. The
loader emits startup diagnostics, never an error screen.

The converter uses Pillow's sequential GIF frame loading so disposal and
transparency are composed before resizing. It uses nearest-neighbor contain
scaling to fit inside 64×64 without stretching; unused space and transparent
pixels become pure black. GIF frame durations are preserved within 20–2000 ms;
shorter and longer values clamp, and absent durations default to 100 ms. It
rejects input above the native frame and total-duration limits. No dithering,
filtering, crop mode, or per-animation brightness is applied.

`AnimationApp` loops one loaded asset for the full carousel dwell. At each
activation it selects the next sorted asset in deterministic round-robin order
and starts at frame 0. It computes `elapsed_monotonic_ms % total_duration_ms`
and chooses the frame directly, even after a large time jump. The selected
asset stays fixed during explicit volume content and its 900 ms dissolve:
presentation pauses the scheduler dwell but does not change the app pointer or
fire activation. Hidden playback time continues. A sole available animation
repeats on its next appearance. Clock and Weather do not override the hook.

### Adding a new animation

From the repository root, create an isolated converter environment and convert
a GIF you have rights to use:

```sh
python3 -m venv /tmp/cube-animation-venv
/tmp/cube-animation-venv/bin/python -m pip install -r tools/requirements-animation.txt
/tmp/cube-animation-venv/bin/python tools/convert_cube_animation.py \
  path/to/new.gif client/assets/animations/new.cubeanim
/tmp/cube-animation-venv/bin/python tools/preview_cube_animations.py \
  client/assets/animations /tmp/cube-animation-preview
```

Review `/tmp/cube-animation-preview/contact-sheet.png` and the per-animation
GIFs there. Keep no more than eight valid `.cubeanim` files in the runtime
directory. During a later deployment, place the asset in this directory and
restart the renderer using the normal deployment procedure to reload the
catalog; adding an asset alone does not require recompiling C++. Do not start a
second renderer against the panel. To recreate the original samples,
run `tools/generate_cube_animation_samples.py` with the source and runtime
directories as its two arguments, then run the preview command above.

## Future apps and testing

To implement a future app, derive from `DisplayApp`, inspect only the context
and an already cached source, and write a complete `Frame64`. Register a stable app object
and add its ID and duration to the native schedule. The scheduler sees only
`Available(context)` and does not know why data is unavailable. Clock + Date and
Weather are the production information apps; Animation is the local
entertainment app. Other app types and remote
configuration remain future work.

Native tests construct contexts and fake apps, render frames, apply the overlay,
compose explicit content and dissolve transitions, then inspect pixels without
RGBMatrix, GPIO, systemd, or a panel. The Python client sends presentation
messages through `client/app/display/transport.py` to the authenticated
`@cube-display` socket. The socket retains its UID check, datagram bounds,
and per-frame receive limit.

`tests/client/native_carousel_test.cc` exercises registry validation and lifetime,
ordered rotation and exact boundaries, per-entry durations, availability changes,
configuration replacement, empty/black behavior, pointer stability, suspension,
long jumps, and the existing content/overlay pipeline with synthetic time.
`tests.client.test_native_content` compiles and runs it without matrix headers.

`tests/client/native_weather_test.cc` checks protocol bounds, availability,
layout, animation, density, and the historical two-app carousel. It optionally
exports 12 scene PPM frames, four animation follow-up frames, and a
four-column contact sheet:

```sh
g++ -std=c++17 -O2 -Wall -Wextra -Werror -I client/native/cube-display \
  tests/client/native_weather_test.cc \
  client/native/cube-display/apps/weather.cc \
  client/native/cube-display/apps/tetris-clock.cc \
  client/native/cube-display/content-mask.cc \
  client/native/cube-display/content-renderer.cc \
  client/native/cube-display/dissolve-transition.cc -o /tmp/cube-weather-test
/tmp/cube-weather-test /tmp/cube-weather-preview
```

`tests/client/native_tetris_clock_test.cc` checks known times and dates, geometry
connectivity and non-overlap, bounded frames, deterministic motion at different
render cadences, completion/stability, changed-digit carries, direct forward and
backward time jumps, failure/recovery, registry resolution, single-entry dwell
renewal, and explicit white content/release over a real clock frame. Python also
audits that production native matrix references remain confined to `cube-display.cc`.

The same test executable optionally exports deterministic 64×64 PPM snapshots
without any matrix library. There is no preview server or additional runtime:

```sh
g++ -std=c++17 -O2 -Wall -Wextra -Werror -I client/native/cube-display \
  tests/client/native_tetris_clock_test.cc \
  client/native/cube-display/apps/tetris-clock.cc \
  client/native/cube-display/content-mask.cc \
  client/native/cube-display/content-renderer.cc \
  client/native/cube-display/dissolve-transition.cc -o /tmp/cube-tetris-clock-test
/tmp/cube-tetris-clock-test /tmp/cube-clock-preview
```

Snapshots cover initial construction, a single-digit minute change, and finished
times spanning all ten digits. Review with nearest-neighbor enlargement if needed.
`tests/client/native_animation_test.cc` validates temporary native assets,
playback boundaries, activation, availability, and the production three-app
schedule. `tests/client/test_animation_converter.py` validates offline GIF
conversion in a development environment with Pillow. Host tests and a candidate
build do not validate physical color balance or panel readability. The animation pipeline does
not install or run the candidate or restart services.
