#include "music.h"

namespace cube_display {
namespace {
// Layout settings (pixels). Keep artwork + gap + controls above voice row 63.
constexpr int kArtworkSize = 58;       // Square viewport; centered horizontally.
constexpr int kArtworkTop = 0;
constexpr int kControlsGap = 2;        // Blank rows between cover and controls.
constexpr int kProgressOffset = 7;     // Bar start relative to the cover's left.
constexpr int kControlsHeight = 3;     // Height of the play/pause glyph.
static_assert(kArtworkSize <= 64 && kArtworkTop >= 0 && kControlsGap >= 0);
static_assert(kArtworkTop + kArtworkSize + kControlsGap + kControlsHeight <= 63);
static_assert(kProgressOffset >= 4 && kProgressOffset < kArtworkSize);
}  // namespace

void MusicApp::Render(const DisplayContext& context, Frame64* frame) {
  frame->fill({});
  if (!Available(context)) return;
  constexpr int art_size = kArtworkSize, controls_height = kControlsHeight;
  constexpr int left = (64 - art_size) / 2;
  constexpr int top = kArtworkTop;
  constexpr int controls_top = top + art_size + kControlsGap;
  constexpr int bar_left = left + kProgressOffset, bar_width = art_size - kProgressOffset;
  if (state_.has_artwork()) {
    // prepare_artwork() already fits/letterboxes into x=8..55, y=6..53.
    // Scale the whole viewport uniformly, retaining all artwork/letterboxing.
    constexpr int source_size = 48, source_left = 8, source_top = 6;
    for (int y = 0; y < art_size; ++y)
      for (int x = 0; x < art_size; ++x)
        PixelAt(*frame, left + x, top + y) =
            state_.artwork()[PixelIndex(
                source_left + (2 * x + 1) * source_size / (2 * art_size),
                source_top + (2 * y + 1) * source_size / (2 * art_size))];
  } else {
    constexpr Rgb note{100, 160, 200};
    // Keep the existing placeholder in the same artwork viewport.
    constexpr int offset = top + (art_size - 60) / 2;
    for (int y = 18; y < 39; ++y) {
      PixelAt(*frame, 27, y + offset) = note;
      PixelAt(*frame, 40, y - 4 + offset) = note;
    }
    for (int x = 27; x <= 40; ++x) PixelAt(*frame, x, 18 + offset) = note;
    for (int y = 36; y < 41; ++y)
      for (int x = 22; x < 28; ++x) {
        PixelAt(*frame, x, y + offset) = note;
        PixelAt(*frame, x + 13, y - 4 + offset) = note;
      }
  }
  constexpr Rgb white{200, 200, 200}, dim{25, 25, 25};
  for (int y = 0; y < controls_height; ++y) {
    PixelAt(*frame, left, controls_top + y) = white;
    if (state_.status() == 1)
      PixelAt(*frame, left + (y == 1 ? 2 : 1), controls_top + y) = white;
    else PixelAt(*frame, left + 3, controls_top + y) = white;
  }
  const auto progress = state_.Progress(context);
  if (state_.duration() > 0 && progress >= 0) {
    const int filled = static_cast<int>(progress * bar_width / state_.duration());
    for (int x = 0; x < bar_width; ++x)
      PixelAt(*frame, bar_left + x, controls_top + 1) = x < filled ? white : dim;
  }
}
}  // namespace cube_display
