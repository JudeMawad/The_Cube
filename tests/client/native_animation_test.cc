#include "animation-asset.h"
#include "apps/animation.h"
#include "carousel-scheduler.h"
#include "content-renderer.h"
#include "display-compositor.h"

#include <cassert>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

using namespace cube_display;
using namespace std::chrono_literals;

namespace {

DisplayContext At(std::chrono::steady_clock::duration time) {
  return {std::chrono::steady_clock::time_point{} + time, 33ms};
}

void Put16(std::vector<std::uint8_t>* data, std::uint16_t value) {
  data->push_back(static_cast<std::uint8_t>(value));
  data->push_back(static_cast<std::uint8_t>(value >> 8));
}

std::vector<std::uint8_t> Asset(const std::vector<std::uint16_t>& durations,
                                 const std::vector<std::uint16_t>& colors) {
  assert(durations.size() == colors.size());
  std::vector<std::uint8_t> result{'C','U','B','E','A','N','I','M'};
  Put16(&result, 1); Put16(&result, 64); Put16(&result, 64);
  Put16(&result, static_cast<std::uint16_t>(durations.size()));
  for (std::size_t i = 0; i < durations.size(); ++i) {
    Put16(&result, durations[i]);
    for (std::size_t pixel = 0; pixel < kPixels; ++pixel) Put16(&result, colors[i]);
  }
  return result;
}

void Write(const std::filesystem::path& path, const std::vector<std::uint8_t>& bytes) {
  std::ofstream output(path, std::ios::binary);
  output.write(reinterpret_cast<const char*>(bytes.data()), bytes.size());
  assert(output.good());
}

void Format(const std::filesystem::path& directory) {
  const auto path = directory / "format.cubeanim";
  auto valid = Asset({50, 120}, {0xf800, 0x001f});
  Write(path, valid);
  AnimationAsset parsed;
  std::string error;
  assert(LoadAnimationFile(path, &parsed, &error) && error.empty());
  assert(parsed.frame_count == 2 && parsed.total_ms == 170);
  assert(parsed.frame_starts[0] == 0 && parsed.frame_starts[1] == 50);
  assert(!LoadAnimationFile(path, nullptr, &error));

  auto reject = [&](std::vector<std::uint8_t> bytes) {
    Write(path, bytes);
    AnimationAsset destination;
    assert(!LoadAnimationFile(path, &destination, &error) && !error.empty());
  };
  auto damaged = valid;
  damaged[0] = 'X'; reject(damaged);                    // magic
  damaged = valid; damaged[8] = 2; reject(damaged);    // version
  damaged = valid; damaged[10] = 63; reject(damaged);  // width
  damaged = valid; damaged[12] = 63; reject(damaged);  // height
  damaged = valid; damaged[14] = 0; reject(damaged);   // zero frames
  damaged = valid; damaged[14] = 65; reject(damaged);  // excessive count
  damaged = valid; damaged.pop_back(); reject(damaged); // truncation
  damaged = valid; damaged.push_back(0); reject(damaged); // trailing byte
  damaged = valid; damaged[16] = 0; damaged[17] = 0; reject(damaged);
  damaged = valid; damaged[16] = 19; damaged[17] = 0; reject(damaged);
  damaged = valid; damaged[16] = 0xd1; damaged[17] = 0x07; reject(damaged); // 2001
  damaged = Asset(std::vector<std::uint16_t>(31, 2000),
                  std::vector<std::uint16_t>(31, 0xffff)); reject(damaged);
  reject(std::vector<std::uint8_t>{});
  std::filesystem::remove(path);
}

class SolidApp final : public DisplayApp {
 public:
  explicit SolidApp(Rgb color) : color_(color) {}
  bool available = true;
  bool Available(const DisplayContext&) const override { return available; }
  void Render(const DisplayContext&, Frame64* frame) override { frame->fill(color_); }
 private:
  Rgb color_;
};

void PlaybackAndActivation(const std::filesystem::path& directory) {
  Write(directory / "a.cubeanim", Asset({50, 120}, {0xf800, 0x001f}));
  Write(directory / "b.cubeanim", Asset({70}, {0x07e0}));
  Write(directory / "bad.cubeanim", std::vector<std::uint8_t>{1, 2, 3});
  AnimationCatalog catalog;
  catalog.LoadDirectory(directory);
  assert(catalog.size() == 2);
  assert(catalog.at(0).name == "a" && catalog.at(1).name == "b");
  AnimationApp animation(catalog);
  SolidApp clock({1, 2, 3});
  DisplayRuntime runtime;
  runtime.SetActiveApp(&animation);
  assert(runtime.Render(At(0ms)).front() == (Rgb{255, 0, 0}));
  assert(runtime.Render(At(49ms)).front() == (Rgb{255, 0, 0}));
  assert(runtime.Render(At(50ms)).front() == (Rgb{0, 0, 255}));
  assert(runtime.Render(At(169ms)).front() == (Rgb{0, 0, 255}));
  assert(runtime.Render(At(170ms)).front() == (Rgb{255, 0, 0}));
  assert(runtime.Render(At(24h + 50ms)).front() == (Rgb{0, 0, 255}));

  // Explicit content covers the app without changing the runtime pointer.
  cube_content::ContentRenderer content;
  cube_content::Mask64 mask{};
  assert(cube_content::TextMask("50%", &mask));
  assert(content.RequestMask(mask));
  content.ApplyPending(true);
  const auto covered = content.Compose(Compose(runtime.Render(At(1s)),
      {CubeState::Thinking, 0, 1}), 900);
  content.Presented(true);
  assert(covered != Frame64{});
  assert(runtime.Render(At(2s)).front() == (Rgb{0, 0, 255}));

  runtime.SetActiveApp(&clock);
  runtime.Render(At(3s));
  runtime.SetActiveApp(&animation);
  assert(runtime.Render(At(4s)).front() == (Rgb{0, 255, 0}));
  assert(runtime.Render(At(5s)).front() == (Rgb{0, 255, 0}));
  runtime.SetActiveApp(&clock);
  runtime.Render(At(6s));
  runtime.SetActiveApp(&animation);
  assert(runtime.Render(At(7s)).front() == (Rgb{255, 0, 0}));

  AnimationCatalog one;
  const auto single = directory / "single";
  std::filesystem::create_directory(single);
  Write(single / "only.cubeanim", Asset({100}, {0xffff}));
  one.LoadDirectory(single);
  assert(one.size() == 1);
  AnimationApp sole(one);
  runtime.SetActiveApp(&sole);
  assert(runtime.Render(At(8s)).front() == (Rgb{255, 255, 255}));
  runtime.SetActiveApp(&clock); runtime.Render(At(9s));
  runtime.SetActiveApp(&sole);
  assert(runtime.Render(At(10s)).front() == (Rgb{255, 255, 255}));
  AnimationCatalog absent;
  absent.LoadDirectory(directory / "missing");
  AnimationApp unavailable(absent);
  assert(!unavailable.Available(At(0ms)));

  const auto crowded = directory / "crowded";
  std::filesystem::create_directory(crowded);
  for (int i = 0; i < 9; ++i)
    Write(crowded / (std::to_string(i) + ".cubeanim"), Asset({100}, {0xffff}));
  AnimationCatalog capped;
  capped.LoadDirectory(crowded);
  assert(capped.size() == kAnimationMaxAssets);
  assert(capped.at(0).name == "0" && capped.at(7).name == "7");
}

void Carousel(const std::filesystem::path& directory) {
  AnimationCatalog catalog;
  catalog.LoadDirectory(directory);
  AnimationApp animation(catalog);
  SolidApp clock({1, 2, 3}), weather({4, 5, 6});
  DisplayAppRegistry registry;
  assert(registry.Register("tetris-clock", clock));
  assert(registry.Register("weather", weather));
  assert(registry.Register(AnimationApp::kId, animation));
  CarouselScheduler scheduler(registry);
  assert(scheduler.Configure({{"tetris-clock", 15s}, {"weather", 15s},
                              {"animation", 10s}}));
  assert(scheduler.Update(At(0s), false) == &clock);
  assert(scheduler.Update(At(15s), false) == &weather);
  assert(scheduler.Update(At(30s), false) == &animation);
  assert(scheduler.Update(At(40s), false) == &clock);

  weather.available = false;
  assert(scheduler.Update(At(55s), false) == &animation);
  assert(scheduler.Update(At(65s), false) == &clock);
  weather.available = true;
  assert(scheduler.Update(At(80s), false) == &weather);

  AnimationCatalog absent;
  AnimationApp unavailable(absent);
  DisplayAppRegistry without_assets;
  without_assets.Register("tetris-clock", clock);
  without_assets.Register("weather", weather);
  without_assets.Register("animation", unavailable);
  CarouselScheduler skipped(without_assets);
  assert(skipped.Configure({{"tetris-clock", 15s}, {"weather", 15s},
                            {"animation", 10s}}));
  assert(skipped.Update(At(0s), false) == &clock);
  assert(skipped.Update(At(15s), false) == &weather);
  assert(skipped.Update(At(30s), false) == &clock);
  weather.available = false;
  assert(skipped.Update(At(45s), false) == &clock);
  assert(skipped.Update(At(60s), false) == &clock);
}

}  // namespace

int main(int argc, char* argv[]) {
  assert(argc == 3);
  const std::filesystem::path directory = argv[1];
  Format(directory);
  PlaybackAndActivation(directory);
  Carousel(directory);
  AnimationCatalog bundled;
  bundled.LoadDirectory(argv[2]);
  std::size_t expected = 0;
  for (const auto& entry : std::filesystem::directory_iterator(argv[2]))
    if (entry.is_regular_file() && entry.path().extension() == ".cubeanim") ++expected;
  assert(bundled.size() == expected);  // Every distributed asset must load.
  // Parser/playback behavior above uses synthetic fixtures, independent of art licensing.
  std::cout << "Native animation assertions passed\n";
}
