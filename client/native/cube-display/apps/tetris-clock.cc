#include "tetris-clock.h"
#include "tetris-clock-geometry.h"
#include "../pixel-font.h"

#include <algorithm>

namespace cube_display {
namespace {
using namespace tetris_clock;

// Spread each digit's fixed targets over 2.8 seconds, regardless of piece count.
// Tune here after panel review. Digits build concurrently; each piece falls rigidly.
constexpr auto kAssemblyTime = std::chrono::milliseconds(2800);
constexpr auto kFallTime = std::chrono::milliseconds(600);
constexpr int kDateWidth = 10 * pixel_font::kAdvance - 1;
constexpr int kDateTextX = (kWidth - kDateWidth) / 2 + kDateTextXOffset;
constexpr int kDateTextY = kDateY +
    (kDateStripeHeight - pixel_font::kHeight) / 2 + kDateTextYOffset;
static_assert(kDateY >= kTop + kRows * kBlock);
static_assert(kDateY + kDateStripeHeight <= kHeight - 1); // preserve overlay row
static_assert(kDateTextX >= 0 && kDateTextX + kDateWidth <= kWidth);
static_assert(kDateTextY >= kDateY &&
              kDateTextY + pixel_font::kHeight <= kDateY + kDateStripeHeight);
constexpr std::array<std::string_view, 7> kWeekdays{
    "SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};
constexpr std::array<std::string_view, 12> kMonths{
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"};

bool Valid(const std::tm& local) {
  return local.tm_hour >= 0 && local.tm_hour < 24 && local.tm_min >= 0 && local.tm_min < 60 &&
      local.tm_wday >= 0 && local.tm_wday < 7 && local.tm_mon >= 0 && local.tm_mon < 12 &&
      local.tm_mday >= 1 && local.tm_mday <= 31;
}

void Block(Frame64& frame, int x, int y, Rgb color) {
  for (int dy = 0; dy < kBlock; ++dy)
    for (int dx = 0; dx < kBlock; ++dx)
      if (x + dx >= 0 && x + dx < kWidth && y + dy >= 0 && y + dy < kHeight)
        PixelAt(frame, x + dx, y + dy) = color;
}
}  // namespace

bool SystemLocalClock::Read(std::tm* local) const {
  const auto now = std::time(nullptr);
  // localtime_r need not refresh timezone state itself. Observe system timezone
  // changes as well as wall-clock corrections without requiring a renderer restart.
  tzset();
  return now != std::time_t(-1) && localtime_r(&now, local) != nullptr;
}

void TetrisClockApp::Render(const DisplayContext& context, Frame64* frame) {
  frame->fill({});
  std::tm local{};
  // Stay selectable on transient conversion failure, show black, and retry next
  // render. Preserve targets/start times so recovery does not reset good digits.
  if (!clock_.Read(&local) || !Valid(local)) return;

  const std::array<int, 4> target{{local.tm_hour / 10, local.tm_hour % 10,
                                  local.tm_min / 10, local.tm_min % 10}};
  for (std::size_t i = 0; i < digits_.size(); ++i) {
    if (digits_[i] != target[i]) {
      digits_[i] = target[i];
      started_[i] = context.now;
    }
    const auto& digit = kDigits[digits_[i]];
    const auto age = std::max(context.now - started_[i], decltype(context.elapsed)::zero());
    for (int p = 0; p < digit.count; ++p) {
      const auto delay = p * (kAssemblyTime - kFallTime) / (digit.count - 1);
      if (age < delay) continue;
      const auto falling = age - delay;
      const auto& piece = digit.pieces[p];
      int bottom = 0;
      for (const auto& cell : piece.cells) bottom = std::max(bottom, cell.y + 1);
      const int distance = kTop + bottom * kBlock; // start entirely above the frame
      int offset = 0;
      if (falling < kFallTime) {
        const auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(falling).count();
        offset = distance - static_cast<int>(distance * ms / kFallTime.count());
      }
      const auto color = kPalette[(p + digits_[i] + i * 2) % kPalette.size()];
      for (const auto& cell : piece.cells)
        Block(*frame, kDigitX[i] + kClockXOffset + cell.x * kBlock,
              kTop + cell.y * kBlock - offset, color);
    }
  }

  const std::array<int, 3> date_key{{local.tm_wday, local.tm_mday, local.tm_mon}};
  if (date_key != date_key_) {
    date_key_ = date_key;
    std::copy(kWeekdays[local.tm_wday].begin(), kWeekdays[local.tm_wday].end(), date_.begin());
    date_[3] = date_[6] = ' ';
    date_[4] = '0' + local.tm_mday / 10;
    date_[5] = '0' + local.tm_mday % 10;
    std::copy(kMonths[local.tm_mon].begin(), kMonths[local.tm_mon].end(), date_.begin() + 7);
  }
  for (int y : kColonY) Block(*frame, kColonX + kClockXOffset, y, kNeutral);
  for (int y = kDateY; y < kDateY + kDateStripeHeight; ++y)
    for (int x = 0; x < kWidth; ++x)
      PixelAt(*frame, x, y) = kDateStripeColor;
  pixel_font::Draw(*frame, date_text(), kDateTextX, kDateTextY, {});
}

}  // namespace cube_display
