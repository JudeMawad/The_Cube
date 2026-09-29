#include "content-control.h"
#include "display-compositor.h"
#include "display-runtime.h"

#include <algorithm>
#include <cassert>
#include <cmath>
#include <iostream>
#include <stdexcept>

using namespace cube_content;

namespace {
Mask64 Text(const char* value) {
  Mask64 mask{};
  assert(TextMask(value, &mask));
  return mask;
}

Frame64 WhiteMask(const Mask64& mask) {
  Frame64 result{};
  for (std::size_t i = 0; i < kPixels; ++i)
    if (mask[i]) result[i] = {255, 255, 255};
  return result;
}

Frame64 Tick(ContentRenderer& renderer, const Frame64& base, double ms = 33,
             bool visible = true) {
  renderer.ApplyPending(visible);
  const Frame64 frame = renderer.Compose(base, ms);
  renderer.Presented(visible);
  return frame;
}

class TestApp final : public cube_display::DisplayApp {
 public:
  bool available = true;
  int renders = 0;
  bool Available(const cube_display::DisplayContext&) const override { return available; }
  void Render(const cube_display::DisplayContext&, Frame64* frame) override {
    ++renders;
    frame->fill({20, 30, 40});
  }
};

void Foundation() {
  static_assert(cube_display::kWidth == 64 && cube_display::kHeight == 64);
  static_assert(cube_display::kPixels == 4096);
  Frame64 frame{};
  assert(frame == Frame64{});
  cube_display::PixelAt(frame, 63, 63) = {1, 2, 3};
  assert(frame[4095] == (Rgb{1, 2, 3}));
  bool threw = false;
  try { cube_display::PixelAt(frame, 64, 63); }
  catch (const std::out_of_range&) { threw = true; }
  assert(threw);

  cube_display::DisplayRuntime runtime;
  const cube_display::DisplayContext context{std::chrono::steady_clock::now(),
                                               std::chrono::milliseconds(33)};
  assert(runtime.Render(context) == Frame64{});
  TestApp app;
  runtime.SetActiveApp(&app);
  assert(runtime.Render(context)[0] == (Rgb{20, 30, 40}));
  assert(app.renders == 1);
  app.available = false;
  assert(runtime.Render(context) == Frame64{} && app.renders == 1);
  runtime.SetActiveApp(nullptr);
  assert(runtime.Render(context) == Frame64{});
}

void Overlay() {
  using namespace cube_display;
  Frame64 base{};
  base.fill({4, 5, 6});
  const auto idle = Compose(base, {CubeState::Idle, 0});
  assert(idle == base);
  for (const auto state : {CubeState::Listening, CubeState::Thinking,
                           CubeState::Speaking, CubeState::Followup}) {
    const auto frame = Compose(base, {state, 0.5, 1.0});
    for (int y = 0; y < kHeight - 1; ++y)
      for (int x = 0; x < kWidth; ++x)
        assert(frame[PixelIndex(x, y)] == base[PixelIndex(x, y)]);
    for (int x = 0; x < kWidth; ++x) {
      const auto pixel = frame[PixelIndex(x, kHeight - 1)];
      assert(pixel.r == pixel.g && pixel.g == pixel.b && pixel.r >= 112);
    }
    assert(!(frame.back() == base.back()));
  }
  const auto start = Compose(base, {CubeState::Listening, 0, 0});
  assert(start == base);
  const auto entering = Compose(base, {CubeState::Listening, 0, 0.2});
  assert(entering[PixelIndex(31, 63)] == (Rgb{112, 112, 112}));
  assert(entering[PixelIndex(0, 63)] == base[PixelIndex(0, 63)]);
  assert(entering[PixelIndex(31, 63)] == entering[PixelIndex(32, 63)]);
  bool bright_head = false;
  for (int x = 0; x < kWidth; ++x)
    bright_head |= entering[PixelIndex(x, 63)] == (Rgb{255, 255, 255});
  assert(bright_head);
  const auto listening = Compose(base, {CubeState::Listening, 0, 0.65});
  const auto followup = Compose(base, {CubeState::Followup, 0, 10});
  assert(listening == followup);
  for (int x = 0; x < kWidth; ++x)
    assert(listening[PixelIndex(x, 63)] == (Rgb{112, 112, 112}));

  const auto thinking_a = Compose(base, {CubeState::Thinking, 0, 0.4});
  const auto thinking_b = Compose(base, {CubeState::Thinking, 0, 0.8});
  int peak_a = 0, peak_b = 0;
  for (int x = 1; x < kWidth; ++x) {
    if (thinking_a[PixelIndex(x, 63)].r > thinking_a[PixelIndex(peak_a, 63)].r) peak_a = x;
    if (thinking_b[PixelIndex(x, 63)].r > thinking_b[PixelIndex(peak_b, 63)].r) peak_b = x;
  }
  assert(peak_a < peak_b);
  assert(thinking_a[PixelIndex(peak_a, 63)] == (Rgb{255, 255, 255}));
  assert(thinking_b[PixelIndex(peak_b, 63)] == (Rgb{255, 255, 255}));
  assert(Compose(base, {CubeState::Speaking, -1}).back() == (Rgb{112, 112, 112}));
  assert(Compose(base, {CubeState::Speaking, 2}).back() == (Rgb{255, 255, 255}));
  assert(Compose(base, {CubeState::Speaking, NAN}).back() == (Rgb{112, 112, 112}));
}

void Masks() {
  const std::pair<const char*, uint64_t> golden[] = {
      {"04:32", 4821365676619242701ULL}, {"23°", 11552106898217653312ULL},
      {"75%", 2894137369031634251ULL}, {"HI", 10115237051426807109ULL}};
  for (const auto& entry : golden) {
    uint64_t hash = 14695981039346656037ULL;
    for (auto pixel : Text(entry.first)) { hash ^= pixel; hash *= 1099511628211ULL; }
    assert(hash == entry.second);
  }
  Mask64 output = Text("HI");
  const auto original = output;
  for (const std::string bad : {"", " ", "hello", "123456", "A B", "é"}) {
    assert(!TextMask(bad, &output));
    assert(output == original);
  }
  for (const char* name : {"check", "warning", "pause"})
    assert(IconMask(name, &output) && ValidMask(output));
}

void Dissolve() {
  Frame64 base{};
  base.fill({12, 34, 56});
  const auto mask = Text("50%");
  ContentRenderer renderer;
  assert(Tick(renderer, base) == base);
  assert(renderer.RequestMask(mask));
  assert(Tick(renderer, base) == base); // Submitted snapshot first.
  assert(renderer.state() == Presentation::FormingContent);
  const auto middle = Tick(renderer, base, 450);
  assert(middle != base && middle != WhiteMask(mask));
  assert(Tick(renderer, base, 450) == WhiteMask(mask));
  assert(renderer.state() == Presentation::Content);
  // Explicit content owns the whole frame, including the overlay pixel.
  assert(Tick(renderer, cube_display::Compose(base, {cube_display::CubeState::Thinking, 0}))
         == WhiteMask(mask));
  assert(renderer.RequestMask(Text("MUTE")));
  const auto snapshot = renderer.presented_frame();
  assert(Tick(renderer, base) == snapshot);
  const auto next = Tick(renderer, base, 900);
  assert(next == WhiteMask(Text("MUTE")));
  renderer.ClearContent();
  assert(Tick(renderer, base) == next);
  assert(Tick(renderer, base, 900) == base);
  assert(renderer.state() == Presentation::Full);

  int min_delay = 450, max_delay = 0;
  for (uint16_t i = 0; i < kPixels; ++i) {
    const int delay = DissolveDelay(i);
    assert(delay >= 0 && delay <= 450 && delay == DissolveDelay(i));
    min_delay = std::min(delay, min_delay);
    max_delay = std::max(delay, max_delay);
  }
  assert(max_delay - min_delay > 200);
  const auto pixel = DissolvePixel({255, 255, 255}, kFullCoverage, {}, 0, 450, 0);
  assert(pixel.coverage < kFullCoverage && pixel.color.r == pixel.color.g);

  ContentRenderer on_black;
  Frame64 black{};
  Tick(on_black, black);
  on_black.RequestMask(mask);
  Tick(on_black, black);
  const auto forming = Tick(on_black, black, 450);
  bool dim = false, lit = false;
  for (std::size_t i = 0; i < kPixels; ++i) if (mask[i]) {
    dim |= forming[i].r < 255;
    lit |= forming[i].r > 0;
  }
  assert(dim && lit);
  assert(Tick(on_black, black, 450) == WhiteMask(mask));
  on_black.ClearContent();
  Tick(on_black, black);
  const auto releasing = Tick(on_black, black, 450);
  dim = lit = false;
  for (std::size_t i = 0; i < kPixels; ++i) if (mask[i]) {
    dim |= releasing[i].r < 255;
    lit |= releasing[i].r > 0;
  }
  assert(dim && lit);
}

void TransitionInterruptions() {
  Frame64 black{};
  const auto first = Text("HI");
  const auto second = Text("23°");
  for (int interrupt_at : {200, 750, 890}) {
    ContentRenderer renderer;
    Tick(renderer, black);
    renderer.RequestMask(first);
    Tick(renderer, black);
    Tick(renderer, black, interrupt_at);
    const auto snapshot = renderer.presented_frame();
    const auto coverage = renderer.presented_coverage();
    renderer.RequestMask(second);
    assert(Tick(renderer, black) == snapshot);
    assert(renderer.presented_coverage() == coverage);
    assert(Tick(renderer, black, 900) == WhiteMask(second));

    renderer.ClearContent();
    assert(Tick(renderer, black) == WhiteMask(second));
    Tick(renderer, black, interrupt_at);
    const auto releasing = renderer.presented_frame();
    renderer.RequestMask(first);
    const auto retargeted = Tick(renderer, black);
    if (renderer.state() == Presentation::MorphingContent)
      assert(retargeted == releasing);
    assert(Tick(renderer, black, 900) == WhiteMask(first));
  }

  // With a black app frame, the old volume effect remains a spatial dissolve:
  // white mask pixels fade at different times, not as one uniform opacity.
  ContentRenderer release;
  release.RequestMask(first);
  Tick(release, black);
  release.ClearContent();
  Tick(release, black);
  const auto start = release.coverage();
  uint16_t previous_max = kFullCoverage;
  for (int step = 0; step < 17; ++step) {
    const auto frame = Tick(release, black, 50);
    uint16_t max_coverage = 0;
    for (std::size_t i = 0; i < kPixels; ++i) {
      assert(release.coverage()[i] <= start[i]);
      if (!first[i]) assert(frame[i] == Rgb{} && release.coverage()[i] == 0);
      max_coverage = std::max(max_coverage, release.coverage()[i]);
    }
    assert(max_coverage <= previous_max);
    previous_max = max_coverage;
  }
  assert(Tick(release, black, 50) == black);
}

void HiddenAndProtocol() {
  ContentRenderer renderer;
  Frame64 black{};
  Tick(renderer, black);
  assert(ApplyContentCommand("control show_text HI", renderer, true) == "{\"accepted\":true}");
  assert(Tick(renderer, black, 0, false) == WhiteMask(Text("HI")));
  assert(renderer.state() == Presentation::Content);
  assert(renderer.presented_frame() == Frame64{});
  assert(Tick(renderer, black) == WhiteMask(Text("HI")));
  for (const std::string bad : {"control show_text ", "control show_icon unknown",
                                 "control set_transition_style smooth", "control shutdown"})
    assert(ApplyContentCommand(bad, renderer, true) == "{}");
  assert(ApplyContentCommand("control get_content_status", renderer, false)
         == "{\"available\":false}");
  renderer.ClearContent();
  assert(Tick(renderer, black, 0, false) == black);
}
}  // namespace

int main() {
  static_assert(sizeof(ContentRenderer) + sizeof(Frame64) < 320 * 1024);
  Foundation(); Overlay(); Masks(); Dissolve(); TransitionInterruptions();
  HiddenAndProtocol();
  std::cout << "Native content assertions passed\n";
}
