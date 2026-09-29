// Hardware-free behavioral tests and optional deterministic PPM snapshot export.
#include "apps/tetris-clock.h"
#include "apps/tetris-clock-geometry.h"
#include "carousel-scheduler.h"
#include "display-compositor.h"
#include "content-renderer.h"
#include "pixel-font.h"

#include <algorithm>
#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <set>
#include <string>

using namespace cube_display;
using namespace std::chrono_literals;
namespace tc = cube_display::tetris_clock;

namespace {
struct FakeClock final : LocalClockSource {
  std::tm local{};
  bool valid = true;
  mutable int reads = 0;
  FakeClock(int hour = 12, int minute = 34) {
    local.tm_hour = hour;
    local.tm_min = minute;
    local.tm_year = 126;
    local.tm_mon = 8;
    local.tm_mday = 26;
    local.tm_wday = 6;
  }
  bool Read(std::tm* value) const override {
    ++reads;
    *value = local;
    return valid;
  }
};

DisplayContext At(int64_t ms) {
  // Deliberately irrelevant elapsed value: animation must use start timestamps.
  return {std::chrono::steady_clock::time_point{} + std::chrono::milliseconds(ms), 7ms};
}

Frame64 Render(TetrisClockApp& app, int64_t ms) {
  Frame64 frame;
  frame.fill({1, 2, 3}); // app must overwrite caller storage, including the background
  app.Render(At(ms), &frame);
  return frame;
}

bool Black(const Frame64& frame) {
  return std::all_of(frame.begin(), frame.end(), [](Rgb c) { return c == Rgb{}; });
}

bool SameDigit(const Frame64& a, const Frame64& b, int position) {
  for (int y = 0; y < tc::kTop + tc::kRows * tc::kBlock; ++y)
    for (int x = tc::kDigitX[position] + tc::kClockXOffset;
         x < tc::kDigitX[position] + tc::kClockXOffset + tc::kColumns * tc::kBlock; ++x)
      if (!(PixelAt(a, x, y) == PixelAt(b, x, y))) return false;
  return true;
}

int DigitPixels(const Frame64& frame, int position) {
  int count = 0;
  for (int y = 0; y < tc::kTop + tc::kRows * tc::kBlock; ++y)
    for (int x = tc::kDigitX[position] + tc::kClockXOffset;
         x < tc::kDigitX[position] + tc::kClockXOffset + tc::kColumns * tc::kBlock; ++x)
      count += !(PixelAt(frame, x, y) == Rgb{});
  return count;
}

std::array<int, 2> ClockBounds(const Frame64& frame) {
  int left = kWidth;
  int right = -1;
  for (int y = tc::kTop; y < tc::kTop + tc::kRows * tc::kBlock; ++y)
    for (int x = 0; x < kWidth; ++x)
      if (!(PixelAt(frame, x, y) == Rgb{})) {
        left = std::min(left, x);
        right = std::max(right, x);
      }
  assert(right >= left);
  return {left, right};
}

void Geometry() {
  std::set<std::array<bool, tc::kColumns * tc::kRows>> silhouettes;
  for (const auto& digit : tc::kDigits) {
    assert(digit.count > 1 && digit.count <= static_cast<int>(digit.pieces.size()));
    std::array<bool, tc::kColumns * tc::kRows> occupied{};
    int leftmost = tc::kColumns;
    int rightmost = -1;
    for (int p = 0; p < digit.count; ++p) {
      const auto& cells = digit.pieces[p].cells;
      std::array<bool, 4> reached{{true, false, false, false}};
      for (int iteration = 0; iteration < 4; ++iteration)
        for (int a = 0; a < 4; ++a)
          for (int b = 0; b < 4; ++b)
            if (reached[a] && std::abs(cells[a].x - cells[b].x) +
                std::abs(cells[a].y - cells[b].y) == 1) reached[b] = true;
      assert(std::all_of(reached.begin(), reached.end(), [](bool v) { return v; }));
      for (const auto& cell : cells) {
        assert(cell.x >= 0 && cell.x < tc::kColumns && cell.y >= 0 && cell.y < tc::kRows);
        leftmost = std::min(leftmost, cell.x);
        rightmost = std::max(rightmost, cell.x);
        auto& used = occupied[cell.y * tc::kColumns + cell.x];
        assert(!used); // no cell shared within a tetromino or across final pieces
        used = true;
      }
    }
    assert(leftmost == 0 && rightmost == tc::kColumns - 1);
    assert(silhouettes.insert(occupied).second); // all ten digits distinct
  }
  for (std::size_t i = 0; i < tc::kDigitX.size(); ++i) {
    assert(tc::kDigitX[i] >= 0 && tc::kDigitX[i] + tc::kColumns * tc::kBlock <= kWidth);
    if (i) assert(tc::kDigitX[i - 1] + tc::kColumns * tc::kBlock < tc::kDigitX[i]);
  }
  assert(tc::kDigitX[1] + tc::kColumns * tc::kBlock < tc::kColonX);
  assert(tc::kColonX + tc::kBlock < tc::kDigitX[2]);
  for (int y : tc::kColonY) assert(y >= tc::kTop && y + tc::kBlock <= tc::kTop + tc::kRows * tc::kBlock);
  assert(tc::kTop + tc::kRows * tc::kBlock < tc::kDateY);
  assert(pixel_font::kHeight < tc::kDateStripeHeight);
  assert(10 * pixel_font::kAdvance - 1 <= kWidth);
  assert(tc::kDateY + tc::kDateStripeHeight <= kHeight - 1);
  assert(tc::kDigitX.front() + tc::kClockXOffset >= 0);
  assert(tc::kDigitX.back() + tc::kClockXOffset + tc::kColumns * tc::kBlock <= kWidth);
}

void ClockAndDate() {
  const std::array<std::array<int, 4>, 5> expected{{
      {{0, 0, 0, 0}}, {{0, 9, 0, 5}}, {{1, 2, 3, 4}}, {{1, 9, 5, 9}}, {{2, 3, 5, 9}}}};
  for (const auto& digits : expected) {
    FakeClock source(digits[0] * 10 + digits[1], digits[2] * 10 + digits[3]);
    TetrisClockApp app(source);
    Render(app, 0);
    assert(app.target_digits() == digits);
    assert(app.date_text() == "SAT 26 SEP");
    const auto frame = Render(app, 3000);
    assert((ClockBounds(frame) == std::array<int, 2>{3 + tc::kClockXOffset,
                                                     60 + tc::kClockXOffset}));
    const int date_left = (kWidth - (10 * pixel_font::kAdvance - 1)) / 2 +
        tc::kDateTextXOffset;
    const int date_top = tc::kDateY + (tc::kDateStripeHeight - pixel_font::kHeight) / 2 +
        tc::kDateTextYOffset;
    assert(date_top - tc::kDateY == 3);
    assert(tc::kDateY + tc::kDateStripeHeight -
           (date_top + pixel_font::kHeight) == 3);
    for (int y = tc::kDateY; y < tc::kDateY + tc::kDateStripeHeight; ++y) {
      for (int x = 0; x < kWidth; ++x) {
        const auto pixel = PixelAt(frame, x, y);
        const bool inside_text = x >= date_left && x < date_left + 10 * pixel_font::kAdvance - 1 &&
            y >= date_top && y < date_top + pixel_font::kHeight;
        assert(pixel == tc::kDateStripeColor || (inside_text && pixel == Rgb{}));
      }
    }
    // The date is cut out of the stripe, with a white inset and open glyphs.
    assert(PixelAt(frame, date_left + 1, date_top) == Rgb{}); // top of S
    assert(PixelAt(frame, date_left + 2, date_top) == Rgb{});
    assert(PixelAt(frame, date_left + 1, date_top + 1) == Rgb{}); // two-pixel stroke
    assert(PixelAt(frame, date_left, date_top) == tc::kDateStripeColor);
    assert(PixelAt(frame, date_left + 8, date_top + 2) == tc::kDateStripeColor); // A counter
    assert(PixelAt(frame, 0, tc::kDateY - 1) == Rgb{});
    assert(PixelAt(frame, 0, tc::kDateY + tc::kDateStripeHeight) == Rgb{});
    for (int i = 0; i < 4; ++i)
      assert(DigitPixels(frame, i) == tc::kDigits[digits[i]].count * 4 * tc::kBlock * tc::kBlock);
  }

  FakeClock source;
  TetrisClockApp app(source);
  Render(app, 0);
  const auto before = Render(app, 3000);
  source.local.tm_mday = 27;
  source.local.tm_wday = 0;
  const auto after = Render(app, 4000);
  assert(app.date_text() == "SUN 27 SEP");
  assert(before != after);
  for (int i = 0; i < 4; ++i) assert(SameDigit(before, after, i));

  // All weekday/month letters are actually supported by the tiny font.
  for (char c : std::string("SUNMONTUEWEDTHUFRISATJANFEBMARAPRMAYJUNJULAUGSEPOCTNOVDEC0123456789")) {
    const auto glyph = pixel_font::Glyph(c);
    assert(std::any_of(glyph.begin(), glyph.end(), [](uint8_t row) { return row != 0; }));
  }
  source.local.tm_year = 127;
  source.local.tm_mon = 0;
  source.local.tm_mday = 1;
  source.local.tm_wday = 5;
  Render(app, 5000);
  assert(app.date_text() == "FRI 01 JAN");
}

void AnimationAndFrames() {
  FakeClock source(18, 28); // includes the largest 17-piece digit
  TetrisClockApp fine(source), sparse(source);
  const auto first = Render(fine, 10000);
  assert(Render(sparse, 10000) == first);
  for (int i = 0; i < 4; ++i) assert(DigitPixels(first, i) == 0);
  Frame64 previous = first;
  bool moving = false;
  for (int ms = 10013; ms < 11400; ms += 13) {
    const auto next = Render(fine, ms);
    moving |= previous != next;
    previous = next;
  }
  const auto middle = Render(fine, 11400);
  assert(moving && middle != first);
  assert(middle == Render(sparse, 11400)); // cadence cannot change the frame
  const auto almost = Render(fine, 12799);
  const auto final = Render(fine, 12800);
  assert(almost != final && middle != final);
  assert(final == Render(sparse, 12800));
  assert(final == Render(fine, 1000000000000LL)); // long stalls do not overflow motion math
  assert(final == Render(fine, 12801)); // no accumulated delta state
  source.local.tm_sec = 59;
  assert(final == Render(fine, 15000)); // seconds do not trigger animation

  // Guard storage and constrain every lit pixel throughout an entire animation.
  struct Guarded { uint64_t before = 12345; Frame64 frame{}; uint64_t after = 67890; } guarded;
  TetrisClockApp bounded(source);
  for (int ms = 0; ms <= 3100; ms += 17) {
    bounded.Render(At(ms), &guarded.frame);
    assert(guarded.before == 12345 && guarded.after == 67890);
    assert(guarded.frame.size() == 64 * 64);
    for (int y = 0; y < kHeight; ++y) {
      for (int x = 0; x < kWidth; ++x) {
        bool allowed = false;
        for (int left : tc::kDigitX)
          allowed |= x >= left + tc::kClockXOffset &&
              x < left + tc::kClockXOffset + 12 &&
              y < tc::kTop + tc::kRows * tc::kBlock;
        for (int top : tc::kColonY)
          allowed |= x >= tc::kColonX + tc::kClockXOffset &&
              x < tc::kColonX + tc::kClockXOffset + 2 && y >= top && y < top + 2;
        allowed |= y >= tc::kDateY && y < tc::kDateY + tc::kDateStripeHeight;
        if (!allowed) assert(PixelAt(guarded.frame, x, y) == Rgb{});
      }
    }
  }
  // Public font helper clips safely when reused at an edge.
  Frame64 clipped{};
  pixel_font::Draw(clipped, "SAT 26 SEP", -2, 62, tc::kNeutral);
  assert(!Black(clipped));
}

void Change(int old_h, int old_m, int new_h, int new_m, std::array<bool, 4> changed) {
  FakeClock source(old_h, old_m);
  TetrisClockApp app(source);
  Render(app, 0);
  const auto before = Render(app, 3000);
  source.local.tm_hour = new_h;
  source.local.tm_min = new_m;
  const auto start = Render(app, 4000);
  const auto progress = Render(app, 4700);
  for (int i = 0; i < 4; ++i) {
    if (changed[i]) {
      assert(DigitPixels(start, i) == 0);
      assert(!SameDigit(start, progress, i));
    } else {
      assert(SameDigit(before, start, i));
      assert(SameDigit(before, progress, i));
    }
  }
  FakeClock expected_source(new_h, new_m);
  TetrisClockApp expected(expected_source);
  Render(expected, 0);
  const auto final = Render(app, 6800);
  assert(final == Render(expected, 2800));
  assert(final == Render(app, 19000));
}

void ChangesAndJumps() {
  Change(14, 37, 14, 38, {false, false, false, true});
  Change(14, 39, 14, 40, {false, false, true, true});
  Change(19, 59, 20, 0, {true, true, true, true});
  Change(23, 59, 0, 0, {true, true, true, true});
  Change(1, 2, 23, 58, {true, true, true, true});
  Change(23, 58, 1, 2, {true, true, true, true});
  Change(2, 59, 2, 0, {false, false, true, true}); // DST-style backward change

  FakeClock source(12, 34);
  TetrisClockApp app(source);
  Render(app, 0);
  Render(app, 700);
  source.local.tm_hour = 5;
  source.local.tm_min = 6;
  const int reads = source.reads;
  Render(app, 701); // replace targets even in the middle of construction
  assert(source.reads == reads + 1);
  assert((app.target_digits() == std::array<int, 4>{0, 5, 0, 6}));
  FakeClock expected_source(5, 6);
  TetrisClockApp expected(expected_source);
  Render(expected, 701);
  assert(Render(app, 3501) == Render(expected, 3501));
}

void Availability() {
  SystemLocalClock system;
  std::tm local{};
  assert(system.Read(&local));
  TetrisClockApp production(system);
  assert(production.Available(At(0)));
  assert(!Black(Render(production, 0))); // date and colon already visible

  // POSIX TZ changes are confined to this test process. The production source
  // must refresh conversion state; no synthetic-clock test needs actual sleep.
  const char* old_zone = std::getenv("TZ");
  const bool had_zone = old_zone != nullptr;
  const std::string saved_zone = old_zone ? old_zone : "";
  assert(setenv("TZ", "UTC0", 1) == 0);
  assert(system.Read(&local) && local.tm_gmtoff == 0);
  assert(setenv("TZ", "UTC-2", 1) == 0);
  assert(system.Read(&local) && local.tm_gmtoff == 7200);
  if (had_zone) assert(setenv("TZ", saved_zone.c_str(), 1) == 0);
  else assert(unsetenv("TZ") == 0);
  tzset();

  FakeClock source;
  source.valid = false;
  TetrisClockApp app(source);
  assert(app.Available(At(0))); // transient conversion failure does not churn selection
  assert(Black(Render(app, 0)));
  source.valid = true;
  Render(app, 100);
  const auto stable = Render(app, 2900);
  source.valid = false;
  assert(Black(Render(app, 3000)));
  source.valid = true;
  assert(stable == Render(app, 3100));
  const auto good = source.local;
  for (int field = 0; field < 5; ++field) {
    source.local = good;
    if (field == 0) source.local.tm_hour = 24;
    if (field == 1) source.local.tm_min = -1;
    if (field == 2) source.local.tm_wday = 7;
    if (field == 3) source.local.tm_mon = 12;
    if (field == 4) source.local.tm_mday = 0;
    assert(Black(Render(app, 4000)));
  }
}

void RegistryCarouselAndContent() {
  FakeClock source;
  TetrisClockApp app(source);
  DisplayAppRegistry registry;
  assert(registry.Register(TetrisClockApp::kId, app));
  assert(registry.Find("tetris-clock") == &app);
  CarouselScheduler carousel(registry);
  assert(carousel.Configure({{TetrisClockApp::kId, 15s}}));
  DisplayRuntime runtime;
  DisplayApp* installed = nullptr;
  int installs = 0;
  Frame64 stable;
  for (int ms : {0, 2800, 14999, 15000, 30000, 90000}) {
    auto* selected = carousel.Update(At(ms), false);
    assert(selected == &app);
    if (selected != installed) {
      installed = selected;
      runtime.SetActiveApp(selected);
      ++installs;
    }
    const auto frame = runtime.Render(At(ms));
    if (ms == 2800) stable = frame;
    if (ms > 2800) assert(frame == stable);
  }
  assert(installs == 1);

  cube_content::ContentRenderer content;
  cube_content::Mask64 mask{};
  assert(cube_content::TextMask("50%", &mask));
  assert(content.RequestMask(mask));
  assert(content.ApplyPending(true));
  const auto composed = Compose(stable, {CubeState::Speaking, 0.7, 2});
  content.Compose(composed, 0);
  content.Presented(true);
  const auto explicit_frame = content.Compose(composed, 900);
  for (std::size_t i = 0; i < mask.size(); ++i)
    assert(explicit_frame[i] == (mask[i] ? Rgb{255, 255, 255} : Rgb{}));
  assert(carousel.Update(At(100000), true) == &app);
  assert(runtime.Render(At(100000)) == stable);
  content.Presented(true);
  content.ClearContent();
  content.ApplyPending(true);
  content.Compose(composed, 0);
  content.Presented(true);
  assert(content.Compose(composed, 900) == composed);
  assert(carousel.Update(At(101000), false) == &app);
  assert(runtime.Render(At(101000)) == stable);
}

void WritePpm(const std::filesystem::path& path, const Frame64& frame) {
  std::ofstream out(path, std::ios::binary);
  out << "P6\n64 64\n255\n";
  for (const auto& color : frame) {
    const char bytes[]{static_cast<char>(color.r), static_cast<char>(color.g), static_cast<char>(color.b)};
    out.write(bytes, sizeof(bytes));
  }
  assert(out.good());
}

void Preview(const std::filesystem::path& directory) {
  std::filesystem::create_directories(directory);
  FakeClock source(14, 37);
  TetrisClockApp app(source);
  for (int ms : {0, 400, 800, 1400, 2200, 2800, 15000})
    WritePpm(directory / ("build-" + std::to_string(ms) + ".ppm"), Render(app, ms));
  source.local.tm_min = 38;
  for (int ms : {16000, 16700, 18800})
    WritePpm(directory / ("minute-" + std::to_string(ms) + ".ppm"), Render(app, ms));
  for (const auto& time : std::array<std::array<int, 2>, 6>{{
           {{0, 0}}, {{9, 5}}, {{12, 34}}, {{19, 59}}, {{23, 59}}, {{6, 28}}}}) {
    FakeClock sample(time[0], time[1]);
    TetrisClockApp clock(sample);
    Render(clock, 0);
    WritePpm(directory / ("time-" + std::to_string(time[0]) + "-" +
                         std::to_string(time[1]) + ".ppm"), Render(clock, 2800));
  }
}
}  // namespace

int main(int argc, char** argv) {
  Geometry();
  ClockAndDate();
  AnimationAndFrames();
  ChangesAndJumps();
  Availability();
  RegistryCarouselAndContent();
  if (argc == 2) Preview(argv[1]);
  std::cout << "Native Tetris clock assertions passed\n";
}
