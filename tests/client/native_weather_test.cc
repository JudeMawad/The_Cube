#include "apps/weather.h"
#include "apps/tetris-clock.h"
#include "apps/tetris-clock-geometry.h"
#include "carousel-scheduler.h"
#include "content-renderer.h"
#include "display-compositor.h"

#include <cassert>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

using namespace cube_display;
using namespace std::chrono_literals;

namespace {

DisplayContext At(int64_t milliseconds) {
  return {std::chrono::steady_clock::time_point{} +
              std::chrono::milliseconds(milliseconds), 33ms};
}

Frame64 Draw(int condition, bool day, int probability, int64_t milliseconds,
             int temperature = 185) {
  WeatherState state;
  assert(state.Apply("weather v1 " + std::to_string(temperature) + " " +
      std::to_string(probability) + " " + std::to_string(condition) + " " +
      (day ? "1" : "0") + " 1800", At(0).now));
  WeatherApp app(state);
  Frame64 frame{};
  app.Render(At(milliseconds), &frame);
  return frame;
}

int Count(const Frame64& frame, Rgb color, int y1 = 0, int y2 = 27) {
  int found = 0;
  for (int y = y1; y <= y2; ++y)
    for (int x = 0; x < kWidth; ++x)
      found += frame[PixelIndex(x, y)] == color;
  return found;
}

bool SceneChanged(const Frame64& first, const Frame64& second) {
  for (int y = 0; y <= 27; ++y)
    for (int x = 0; x < kWidth; ++x)
      if (!(first[PixelIndex(x, y)] == second[PixelIndex(x, y)])) return true;
  return false;
}

void AssertTemperatureCentered(const Frame64& frame) {
  int left = 64, right = -1;
  // The degree mark occupies only rows 30–32. Measure the number alone.
  for (int y = 33; y <= 43; ++y)
    for (int x = 0; x < 64; ++x)
      if (!(frame[PixelIndex(x, y)] == Rgb{})) {
        left = std::min(left, x);
        right = std::max(right, x);
      }
  assert(right >= left);
  assert(std::abs(left + right - 63) <= 4);
}

void Protocol() {
  WeatherState state;
  assert(!state.Available(At(0)));
  assert(state.Apply("weather v1 -35 0 0 0 1800", At(0).now));
  assert(state.snapshot().temperature_c10 == -35);
  assert(state.snapshot().precipitation_probability == 0);
  assert(state.Available(At(1799999)));
  assert(!state.Available(At(1800000)));
  assert(state.Apply("weather v1 1000 100 7 1 1", At(2000000).now));
  assert(state.Available(At(2000999)));
  assert(!state.Available(At(2001000)));
  const std::vector<std::string> invalid{
      "weather v2 10 20 0 1 60", "weather v1", "weather v1 1 2 3 4",
      "weather v1 1 2 3 4 5 6", "weather v1 10 101 0 1 60",
      "weather v1 10 -1 0 1 60", "weather v1 10 20 8 1 60",
      "weather v1 10 20 0 2 60", "weather v1 10 20 0 1 0",
      "weather v1 10 20 0 1 1801", "weather v1 1001 20 0 1 60",
      "weather v1 -1001 20 0 1 60", "weather v1 01 20 0 1 60",
      "weather v1 -0 20 0 1 60", "weather v1 +1 20 0 1 60",
      "weather v1 1 20 0 1 60 ", "weather v1 1 20 0 1 999999999999999"};
  for (const auto& message : invalid) {
    assert(!state.Apply(message, At(3000000).now));
    assert(state.snapshot().temperature_c10 == 1000);
    assert(!state.Available(At(3000000)));
  }
  assert(std::string("weather v1 -1000 100 7 1 1800").size() < 128);
}

void LayoutAndMotion() {
  for (int condition = 0; condition <= 7; ++condition) {
    const auto frame = Draw(condition, true, 60, 1000);
    assert(Count(frame, {}) < 64 * 28);
    for (int y = 0; y <= 27; ++y)
      for (int x : {0, 1, 2, 3, 60, 61, 62, 63})
        assert(frame[PixelIndex(x, y)] == Rgb{});
    for (int y : {28, 29, 44, 45, 46, 47, 63})
      for (int x = 0; x < 64; ++x) assert(frame[PixelIndex(x, y)] == Rgb{});
    for (int y = 30; y <= 43; ++y)
      for (int x = 0; x < 64; ++x)
        if (!(frame[PixelIndex(x, y)] == Rgb{})) assert(x >= 3 && x <= 60);
    static_assert(tetris_clock::kDateY == 48 && tetris_clock::kDateStripeHeight == 15);
    int text_pixels = 0;
    for (int y = tetris_clock::kDateY;
         y < tetris_clock::kDateY + tetris_clock::kDateStripeHeight; ++y)
      for (int x = 0; x < 64; ++x)
        if (frame[PixelIndex(x, y)] == Rgb{}) {
          ++text_pixels;
          assert(y >= 51 && y <= 59 && x >= 5 && x <= 58);
        } else assert(frame[PixelIndex(x, y)] == (Rgb{48, 128, 255}));
    assert(text_pixels > 0);
  }
  const auto negative = Draw(0, true, 0, 1000, -35);
  const auto positive = Draw(0, true, 0, 1000, 35);
  AssertTemperatureCentered(negative);
  AssertTemperatureCentered(positive);
  AssertTemperatureCentered(Draw(0, true, 0, 1000, -1000));
  AssertTemperatureCentered(Draw(0, true, 100, 1000, 1000));
  assert(SceneChanged(Draw(0, true, 0, 1000), Draw(0, true, 0, 1900)));
  assert(SceneChanged(Draw(0, false, 0, 1000), Draw(0, false, 0, 2300)));
  assert(SceneChanged(Draw(1, true, 0, 1000), Draw(1, true, 0, 3500)));
  assert(SceneChanged(Draw(1, false, 0, 1000), Draw(1, false, 0, 3500)));
  assert(SceneChanged(Draw(2, true, 0, 1000), Draw(2, true, 0, 4500)));
  assert(SceneChanged(Draw(3, true, 60, 1000), Draw(3, true, 60, 1300)));
  assert(SceneChanged(Draw(4, true, 60, 1000), Draw(4, true, 60, 1180)));
  assert(!SceneChanged(Draw(3, true, 60, 1020), Draw(3, true, 60, 1080)));
  assert(SceneChanged(Draw(4, true, 60, 1020), Draw(4, true, 60, 1080)));
  assert(SceneChanged(Draw(6, true, 60, 1000), Draw(6, true, 60, 1600)));
  assert(SceneChanged(Draw(7, true, 0, 1000), Draw(7, true, 0, 5000)));
  assert(SceneChanged(Draw(5, true, 60, 8000), Draw(5, true, 60, 8300)));
  assert(Count(Draw(5, true, 60, 8000), {255, 241, 120}) > 0);
  assert(Count(Draw(5, true, 60, 8300), {255, 241, 120}) == 0);
  assert(Count(Draw(3, true, 20, 1000), {42, 182, 255}) <
         Count(Draw(3, true, 60, 1000), {42, 182, 255}));
  assert(Count(Draw(3, true, 60, 1000), {42, 182, 255}) <
         Count(Draw(3, true, 90, 1000), {42, 182, 255}));
  assert(Count(Draw(6, true, 20, 1000), {230, 251, 255}) <
         Count(Draw(6, true, 60, 1000), {230, 251, 255}));
  assert(Count(Draw(6, true, 60, 1000), {230, 251, 255}) <
         Count(Draw(6, true, 90, 1000), {230, 251, 255}));
  assert(Count(Draw(4, true, 60, 1000), {74, 224, 255}) >
         Count(Draw(3, true, 60, 1000), {42, 182, 255}));
  assert(negative != positive);
  assert(Draw(0, true, 60, 1000) == Draw(0, true, 60, 1000));
  assert(Draw(3, true, 60, 1000) == Draw(3, true, 60, 1000));
}

class FixedClock : public LocalClockSource {
 public:
  bool Read(std::tm* value) const override {
    value->tm_hour = 12; value->tm_min = 34;
    value->tm_mday = 26; value->tm_mon = 8; value->tm_wday = 6;
    return true;
  }
};

void Carousel() {
  FixedClock source;
  TetrisClockApp clock(source);
  WeatherState state;
  WeatherApp weather(state);
  DisplayAppRegistry registry;
  assert(registry.Register(TetrisClockApp::kId, clock));
  assert(registry.Register(WeatherApp::kId, weather));
  CarouselScheduler scheduler(registry);
  assert(scheduler.Configure({{"tetris-clock", 15s}, {"weather", 15s}}));
  assert(scheduler.Update(At(0), false) == &clock);
  assert(scheduler.Update(At(15000), false) == &clock);
  assert(state.Apply("weather v1 180 60 3 1 1800", At(16000).now));
  assert(scheduler.Update(At(16000), false) == &clock);
  assert(scheduler.Update(At(29999), false) == &clock);
  assert(scheduler.Update(At(30000), false) == &weather);
  assert(scheduler.Update(At(45000), false) == &clock);
  // Explicit content pauses the selected app's dwell.
  assert(scheduler.Update(At(50000), true) == &clock);
  assert(scheduler.Update(At(60000), true) == &clock);
  assert(scheduler.Update(At(60000), false) == &clock);
  assert(scheduler.Update(At(70000), false) == &weather);
}

void Save(const Frame64& frame, const std::filesystem::path& path) {
  std::ofstream output(path, std::ios::binary);
  output << "P6\n64 64\n255\n";
  for (const auto& pixel : frame) {
    output.put(static_cast<char>(pixel.r));
    output.put(static_cast<char>(pixel.g));
    output.put(static_cast<char>(pixel.b));
  }
  assert(output.good());
}

void Previews(const std::filesystem::path& directory) {
  std::filesystem::create_directories(directory);
  struct Case { const char* name; int condition; bool day; int probability; int64_t time; };
  constexpr Case samples[] = {
      {"clear-day", 0, true, 0, 1000}, {"clear-night", 0, false, 0, 1000},
      {"partly-day", 1, true, 20, 1000}, {"partly-night", 1, false, 20, 3500},
      {"cloudy", 2, true, 20, 4500}, {"rain-20", 3, true, 20, 1000},
      {"rain-60", 3, true, 60, 1300}, {"rain-90", 3, true, 90, 1600},
      {"heavy-rain", 4, true, 90, 1000}, {"thunderstorm", 5, true, 90, 8000},
      {"snow", 6, true, 60, 1600}, {"fog", 7, true, 20, 5000}};
  constexpr int columns = 4, rows = 3;
  std::ofstream sheet(directory / "contact-sheet.ppm", std::ios::binary);
  sheet << "P6\n" << columns * 64 << ' ' << rows * 64 << "\n255\n";
  std::array<Frame64, columns * rows> frames{};
  for (std::size_t i = 0; i < frames.size(); ++i) {
    frames[i] = Draw(samples[i].condition, samples[i].day, samples[i].probability,
                     samples[i].time);
    Save(frames[i], directory / (std::string(samples[i].name) + ".ppm"));
  }
  for (int y = 0; y < rows * 64; ++y)
    for (int x = 0; x < columns * 64; ++x) {
      const auto pixel = frames[(y / 64) * columns + x / 64][PixelIndex(x % 64, y % 64)];
      sheet.put(static_cast<char>(pixel.r));
      sheet.put(static_cast<char>(pixel.g));
      sheet.put(static_cast<char>(pixel.b));
    }
  assert(sheet.good());
  Save(Draw(0, true, 0, 1900), directory / "clear-day-next.ppm");
  Save(Draw(3, true, 60, 1900), directory / "rain-60-next.ppm");
  Save(Draw(6, true, 60, 2400), directory / "snow-next.ppm");
  Save(Draw(7, true, 20, 9000), directory / "fog-next.ppm");
}

}  // namespace

int main(int argc, char* argv[]) {
  Protocol();
  LayoutAndMotion();
  Carousel();
  if (argc == 2) Previews(argv[1]);
  std::cout << "Native weather assertions passed\n";
}
